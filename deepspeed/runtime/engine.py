'''
Copyright 2019 The Microsoft DeepSpeed Team
'''

import os
import torch
import warnings
import hashlib
import torch.distributed as dist

from torch.nn.modules import Module
from torch.distributed.distributed_c10d import _get_global_rank
from tensorboardX import SummaryWriter

from deepspeed.runtime.zero.stage2 import FP16_DeepSpeedZeroOptimizer
from deepspeed.runtime.zero.stage1 import FP16_DeepSpeedZeroOptimizer_Stage1
from deepspeed.runtime.zero.utils import is_zero_supported_optimizer
from deepspeed.runtime.activation_checkpointing import checkpointing as activation_checkpointing
from deepspeed.runtime.fp16.fused_optimizer import FP16_Optimizer
from deepspeed.runtime.fp16.unfused_optimizer import FP16_UnfusedOptimizer
from deepspeed.runtime.config import DeepSpeedConfig, DEEPSPEED_OPTIMIZERS, \
    ADAM_OPTIMIZER, ADAMW_OPTIMIZER, LAMB_OPTIMIZER, ONEBIT_ADAM_OPTIMIZER, \
    TORCH_ADAM_PARAM

from deepspeed.runtime.dataloader import DeepSpeedDataLoader
from deepspeed.runtime.constants import \
    ROUTE_TRAIN, ROUTE_PREDICT, ROUTE_EVAL, \
    PLD_THETA, PLD_GAMMA
from deepspeed.runtime.zero.constants import \
    ZERO_OPTIMIZATION_OPTIMIZER_STATES, ZERO_OPTIMIZATION_GRADIENTS
from deepspeed.runtime.csr_tensor import CSRTensor
import deepspeed.runtime.lr_schedules as lr_schedules
from deepspeed.utils import logger, log_dist, init_distributed
from deepspeed.utils.timer import ThroughputTimer, SynchronizedWallClockTimer
from deepspeed.runtime.progressive_layer_drop import ProgressiveLayerDrop

from .pipe.module import PipelineModule
from .utils import ensure_directory_exists
from ..ops.op_builder import UtilsBuilder
from ..ops.adam import DeepSpeedCPUAdam
from ..ops.adam import FusedAdam

from deepspeed.profiling.flops_profiler.profiler import FlopsProfiler

MEMORY_OPT_ALLREDUCE_SIZE = 500000000

try:
    from apex import amp
except ImportError:
    # Fail silently so we don't spam logs unnecessarily if user isn't using amp
    pass


def split_half_float_double_csr(tensors):
    dtypes = [
        "torch.cuda.HalfTensor",
        "torch.cuda.FloatTensor",
        "torch.cuda.DoubleTensor",
        CSRTensor.type()
    ]
    buckets = []
    for i, dtype in enumerate(dtypes):
        bucket = [t for t in tensors if t.type() == dtype]
        if bucket:
            buckets.append((dtype, bucket))
    return buckets


def _initialize_parameter_parallel_groups(parameter_parallel_size=None):
    data_parallel_size = int(dist.get_world_size())
    if parameter_parallel_size is None:
        parameter_parallel_size = int(data_parallel_size)
    logger.info("data_parallel_size: %s, parameter_parallel_size: %s",
                data_parallel_size,
                parameter_parallel_size)
    assert data_parallel_size % parameter_parallel_size == 0, \
        'world size should be divisible by parameter parallel size'
    rank = dist.get_rank()
    my_group = None
    for i in range(dist.get_world_size() // parameter_parallel_size):
        ranks = range(i * parameter_parallel_size, (i + 1) * parameter_parallel_size)
        group = torch.distributed.new_group(ranks)
        if rank in ranks:
            my_group = group
    return my_group


def print_configuration(args, name):
    logger.info('{}:'.format(name))
    for arg in sorted(vars(args)):
        dots = '.' * (29 - len(arg))
        logger.info('  {} {} {}'.format(arg, dots, getattr(args, arg)))


class DeepSpeedEngine(Module):
    r"""DeepSpeed engine for training.
    """
    def __init__(self,
                 args,
                 model,
                 optimizer=None,
                 model_parameters=None,
                 training_data=None,
                 lr_scheduler=None,
                 mpu=None,
                 dist_init_required=None,
                 collate_fn=None,
                 config_params=None):
        super(DeepSpeedEngine, self).__init__()
        self.client_optimizer = optimizer
        self.client_model_parameters = model_parameters
        self.client_lr_scheduler = lr_scheduler
        self.training_data = training_data
        self.collate_fn = collate_fn
        self.mpu = mpu
        self.data_parallel_group = None
        self.global_steps = 0
        self.global_samples = 0
        self.micro_steps = 0
        self.skipped_steps = 0
        self.gradient_average = True
        self.warn_unscaled_loss = True
        self.config_params = config_params
        self.loaded_checkpoint_mp_world_size = None
        self.loaded_checkpoint_dp_world_size = None
        self.enable_backward_allreduce = True
        self.progressive_layer_drop = None
        self.dist_backend = "nccl"

        if dist_init_required is None:
            dist_init_required = not dist.is_initialized()

        if dist_init_required is False:
            assert dist.is_initialized() is True, "Torch distributed not initialized. Please set dist_init_required to True or initialize before calling deepspeed.initialize()"
        else:
            # Initialize torch distributed if needed
            init_distributed(dist_backend=self.dist_backend)

        self._do_args_sanity_check(args)
        self._configure_with_arguments(args, mpu)
        self._do_sanity_check()

        if mpu is not None:
            assert not self.elasticity_enabled(), "Elasticity is not currently supported" \
                " with model parallelism."

        self._set_distributed_vars()

        if self.tensorboard_enabled() and self.global_rank == 0:
            self.summary_writer = self.get_summary_writer()

        # Configure distributed model
        self._configure_distributed_model(model)

        # Configure wall clock timer
        self.timers = SynchronizedWallClockTimer()

        # Throughput timer
        self.tput_timer = ThroughputTimer(
            batch_size=self.train_micro_batch_size_per_gpu(),
            num_workers=self.dp_world_size,
            steps_per_output=self.steps_per_print(),
            monitor_memory=False)

        if training_data:
            self.training_dataloader = self.deepspeed_io(training_data)
        else:
            self.training_dataloader = None

        # Configure optimizer and scheduler
        self.optimizer = None
        self.lr_scheduler = None
        if model_parameters or optimizer:
            self._configure_optimizer(optimizer, model_parameters)
            self._configure_lr_scheduler(lr_scheduler)
            self._report_progress(0)

        # Bookkeeping for csr support
        self.csr_tensor_module_names = set()
        if self.sparse_gradients_enabled():
            for name, module in self.module.named_modules():
                if isinstance(module, torch.nn.Embedding):
                    self.csr_tensor_module_names.add(name + ".weight")
                    logger.info("Will convert {} to sparse (csr) "
                                "tensor during training".format(name))

        self.save_non_zero_checkpoint = False
        self.save_zero_checkpoint = False
        self._configure_checkpointing(dist_init_required)

        if self.pld_enabled():
            self.progressive_layer_drop = self._configure_progressive_layer_drop()

        if self.global_rank == 0:
            self._config.print('DeepSpeedEngine configuration')
            if self.dump_state():
                print_configuration(self, 'DeepSpeedEngine')

        # Load pre-installed or JIT compile (un)flatten ops
        util_ops = UtilsBuilder().load()
        self.flatten = util_ops.flatten
        self.unflatten = util_ops.unflatten

    def get_batch_info(self):
        """ Get all training batch related settings.

        Returns:
            train_batch_size (int): The effective training batch size. This is the amount of data
                samples that leads to one step of model update.
            train_micro_batch_size_per_gpu (int): Batch size to be processed by one GPU in one
                step (without gradient accumulation).
            gradient_accumulation_steps (int): Number of training steps to accumulate gradients
                before averaging and applying them.
        """
        return self.train_batch_size, self.train_micro_batch_size_per_gpu, self.gradient_accumulation_steps

    def checkpoint_tag_validation_enabled(self):
        return self._config.checkpoint_tag_validation_enabled

    def checkpoint_tag_validation_fail(self):
        return self._config.checkpoint_tag_validation_fail

    def elasticity_enabled(self):
        return self._config.elasticity_enabled

    def pld_enabled(self):
        return self._config.pld_enabled

    def pld_params(self):
        return self._config.pld_params

    def pld_theta(self):
        return self.pld_params()[PLD_THETA]

    def pld_gamma(self):
        return self.pld_params()[PLD_GAMMA]

    def tensorboard_enabled(self):
        return self._config.tensorboard_enabled

    def tensorboard_output_path(self):
        return self._config.tensorboard_output_path

    def tensorboard_job_name(self):
        return self._config.tensorboard_job_name

    def get_summary_writer(self,
                           name="DeepSpeedJobName",
                           base=os.path.join(os.environ["HOME"],
                                             "tensorboard")):
        if self.tensorboard_output_path():
            base_dir = self.tensorboard_output_path()
            job_name = self.tensorboard_job_name()
            log_dir = os.path.join(base_dir, job_name)
        else:
            if self.tensorboard_job_name():
                name = self.tensorboard_job_name()

            # Infrastructure-specific job-id
            if 'DLWS_JOB_ID' in os.environ:
                infra_job_id = os.environ['DLWS_JOB_ID']
            elif 'DLTS_JOB_ID' in os.environ:
                infra_job_id = os.environ['DLTS_JOB_ID']
            else:
                infra_job_id = 'unknown-job-id'

            summary_writer_dir_name = os.path.join(infra_job_id, "logs")
            log_dir = os.path.join(base, summary_writer_dir_name, name)

        os.makedirs(log_dir, exist_ok=True)

        return SummaryWriter(log_dir=log_dir)

    def wall_clock_breakdown(self):
        return self._config.wall_clock_breakdown

    def flops_profiler_enabled(self):
        return self._config.flops_profiler_config.enabled

    def flops_profiler_start_step(self):
        return self._config.flops_profiler_config.start_step

    def flops_profiler_end_step(self):
        return self._config.flops_profiler_config.end_step

    def flops_profiler_module_depth(self):
        return self._config.flops_profiler_config.module_depth

    def flops_profiler_top_modules(self):
        return self._config.flops_profiler_config.top_modules

    def memory_breakdown(self):
        return self._config.memory_breakdown

    def sparse_gradients_enabled(self):
        return self._config.sparse_gradients_enabled

    def train_batch_size(self):
        return self._config.train_batch_size

    def train_micro_batch_size_per_gpu(self):
        return self._config.train_micro_batch_size_per_gpu

    def optimizer_name(self):
        return self.client_optimizer.__class__.__name__ if self.client_optimizer else self._config.optimizer_name

    def optimizer_params(self):
        return self._config.optimizer_params

    def optimizer_legacy_fusion(self):
        return self._config.optimizer_legacy_fusion

    def scheduler_name(self):
        return self._config.scheduler_name

    def scheduler_params(self):
        return self._config.scheduler_params

    def zero_optimization(self):
        return self._config.zero_enabled

    def zero_allow_untested_optimizer(self):
        return self._config.zero_allow_untested_optimizer

    def zero_reduce_scatter(self):
        return self._config.zero_config.reduce_scatter

    def zero_overlap_comm(self):
        return self._config.zero_config.overlap_comm

    def zero_cpu_offload(self):
        return self._config.zero_config.cpu_offload

    def zero_optimization_stage(self):
        return self._config.zero_optimization_stage

    def zero_reduce_bucket_size(self):
        return self._config.zero_config.reduce_bucket_size

    def zero_allgather_bucket_size(self):
        return self._config.zero_config.allgather_bucket_size

    def zero_optimization_partition_gradients(self):
        return self.zero_optimization_stage() >= ZERO_OPTIMIZATION_GRADIENTS

    def zero_contiguous_gradients(self):
        return self._config.zero_config.contiguous_gradients

    def zero_load_from_fp32_weights(self):
        return self._config.zero_config.load_from_fp32_weights

    def zero_elastic_checkpoint(self):
        return self._config.zero_config.elastic_checkpoint

    def fp16_enabled(self):
        return self._config.fp16_enabled

    def amp_enabled(self):
        return self._config.amp_enabled

    def amp_params(self):
        return self._config.amp_params

    def loss_scale(self):
        return self._config.loss_scale

    def gradient_accumulation_steps(self):
        return self._config.gradient_accumulation_steps

    def allreduce_always_fp32(self):
        return self._config.allreduce_always_fp32

    def postscale_gradients(self):
        return not self._config.prescale_gradients

    def gradient_predivide_factor(self):
        return self._config.gradient_predivide_factor

    def steps_per_print(self):
        return self._config.steps_per_print

    def zero_allgather_partitions(self):
        return self._config.zero_config.allgather_partitions

    def dump_state(self):
        return self._config.dump_state

    def gradient_clipping(self):
        return self._config.gradient_clipping

    def dynamic_loss_scale(self):
        return self._config.loss_scale == 0

    def initial_dynamic_scale(self):
        return self._config.initial_dynamic_scale

    def dynamic_loss_scale_args(self):
        return self._config.dynamic_loss_scale_args

    def _configure_lr_scheduler(self, client_lr_scheduler):
        # First check for scheduler in json configuration
        lr_scheduler = self._scheduler_from_config(self.optimizer)
        if lr_scheduler:
            if self.global_rank == 0:
                logger.info(
                    f'DeepSpeed using configured LR scheduler = {self.scheduler_name()}')
            self.lr_scheduler = lr_scheduler
        else:
            if self.global_rank == 0:
                logger.info('DeepSpeed using client LR scheduler')
            self.lr_scheduler = client_lr_scheduler
        log_dist(f'DeepSpeed LR Scheduler = {self.lr_scheduler}', ranks=[0])

    def _configure_checkpointing(self, dist_init_required):

        dp_rank = self.global_rank
        if self.mpu:
            dp_rank = self.mpu.get_data_parallel_rank()

        # only the first data parallel process needs to store the model checkpoint
        self.save_non_zero_checkpoint = (dp_rank == 0)

        if self.zero_optimization():
            param_rank = torch.distributed.get_rank(
                group=self.optimizer.dp_process_group)

            # Only the first parameter parallel process needs to store the
            # optimizer state checkpoints for zero
            self.save_zero_checkpoint = (param_rank == dp_rank)

    def _scheduler_from_config(self, optimizer):
        scheduler_name = self.scheduler_name()
        if scheduler_name is not None:
            if hasattr(lr_schedules, scheduler_name):
                scheduler = getattr(lr_schedules, scheduler_name)
            else:
                assert hasattr(torch.optim.lr_scheduler, scheduler_name), \
                    f"DeepSpeed does not recognize LR scheduler {scheduler_name}"

                scheduler = getattr(torch.optim.lr_scheduler, scheduler_name)

            scheduler_params = self.scheduler_params()
            instantiated_scheduler = scheduler(optimizer, **scheduler_params)
            return instantiated_scheduler
        else:
            return None

    def _set_distributed_vars(self):
        if self.local_rank >= 0:
            torch.cuda.set_device(self.local_rank)
            self.device = torch.device("cuda", self.local_rank)
            self.world_size = dist.get_world_size()
            self.global_rank = dist.get_rank()
        else:
            self.world_size = 1
            self.global_rank = 0
            self.device = torch.device("cuda")

    # Configure based on command line arguments
    def _configure_with_arguments(self, args, mpu):
        if hasattr(args, 'local_rank') and args.local_rank >= 0:
            self.local_rank = args.local_rank
        else:
            self.local_rank = int(os.environ.get("LOCAL_RANK", -1))

        config_file = args.deepspeed_config if hasattr(args,
                                                       'deepspeed_config') else None
        self._config = DeepSpeedConfig(config_file, mpu, param_dict=self.config_params)

    # Validate command line arguments
    def _do_args_sanity_check(self, args):
        if hasattr(args, 'deepscale_config') and args.deepscale_config is not None:
            logger.warning(
                "************ --deepscale_config is deprecated, please use --deepspeed_config ************"
            )
            if hasattr(args, 'deepspeed_config'):
                assert args.deepspeed_config is None, "Not sure how to proceed, we were given both a deepscale_config and deepspeed_config"
            args.deepspeed_config = args.deepscale_config

        local_rank_err = "DeepSpeed requires a command line parameter of --local_rank [int] and/or setting the LOCAL_RANK environment variable."
        if hasattr(args, 'local_rank'):
            assert type(args.local_rank) == int, local_rank_err
            if "LOCAL_RANK" in os.environ and args.local_rank >= 0:
                env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
                assert env_local_rank == args.local_rank, \
                    f"Mismatch in local rank setting, args.local_rank={args.local_rank} but env['LOCAL_RANK']={env_local_rank}."
        else:
            assert "LOCAL_RANK" in os.environ, local_rank_err

        if self.config_params is None:
            assert hasattr(args, 'deepspeed_config') and args.deepspeed_config is not None, \
                'DeepSpeed requires --deepspeed_config to specify configuration file'

            assert os.path.isfile(args.deepspeed_config), \
                'DeepSpeed configuration file: {} is not an existing file'.format(args.deepspeed_config)

    def _is_supported_optimizer(self, optimizer_name):
        return optimizer_name in DEEPSPEED_OPTIMIZERS or \
            getattr(torch.optim, optimizer_name, None) is not None

    # Validate configuration based on command line arguments
    def _do_sanity_check(self):
        if not self.client_optimizer:
            if self.optimizer_name() is not None:
                assert self._is_supported_optimizer(self.optimizer_name()), \
                    '{} is not a supported DeepSpeed Optimizer'.format(self.optimizer_name())

        if self.optimizer_name() == LAMB_OPTIMIZER:
            assert self.dynamic_loss_scale(), \
                'DeepSpeed {} optimizer requires dynamic loss scaling'.format(self.optimizer_name())

    def _broadcast_model(self):
        for p in self.module.parameters():
            if torch.is_tensor(p):
                dist.broadcast(p,
                               self.broadcast_src_rank,
                               group=self.data_parallel_group)

    def _configure_distributed_model(self, model):
        self.module = model
        if self.fp16_enabled():
            self.module.half()
        self.module.to(self.device)

        if self.mpu is None:
            self.data_parallel_group = _initialize_parameter_parallel_groups()
            self.dp_world_size = dist.get_world_size()
            self.mp_world_size = 1
            self.broadcast_src_rank = 0
        else:
            self.data_parallel_group = self.mpu.get_data_parallel_group()
            self.dp_world_size = self.mpu.get_data_parallel_world_size()
            self.mp_world_size = self.mpu.get_model_parallel_world_size()
            self.broadcast_src_rank = _get_global_rank(
                self.mpu.get_data_parallel_group(),
                0)

        if not self.amp_enabled():
            self._broadcast_model()

    # Configure optimizer
    def _configure_optimizer(self, client_optimizer, model_parameters):

        if client_optimizer is not None:
            basic_optimizer = client_optimizer
            if self.global_rank == 0:
                logger.info('Using client Optimizer as basic optimizer')
        else:
            basic_optimizer = self._configure_basic_optimizer(model_parameters)
            if self.global_rank == 0:
                logger.info(
                    'Using DeepSpeed Optimizer param name {} as basic optimizer'.format(
                        self.optimizer_name()))

        if self.global_rank == 0:
            logger.info('DeepSpeed Basic Optimizer = {}'.format(basic_optimizer))

        if self.zero_optimization():
            assert not self.amp_enabled(), "Amp and ZeRO are not currently compatible, please use (legacy) fp16 mode which performs similar to amp opt_mode=O2"
            if not is_zero_supported_optimizer(basic_optimizer):
                assert self.zero_allow_untested_optimizer(), \
                    'You are using an untested ZeRO Optimizer. Please add <"zero_allow_untested_optimizer": true> in the configuration file to use it.'

                if self.global_rank == 0:
                    logger.warning(
                        "**** You are using ZeRO with an untested optimizer, proceed with caution *****"
                    )
            self.optimizer = self._configure_zero_optimizer(basic_optimizer)
        elif self.amp_enabled():
            assert not self.fp16_enabled(), "Cannot enable both amp with (legacy) fp16 mode"
            amp_params = self.amp_params()
            if self.global_rank == 0:
                logger.info(f"Initializing AMP with these params: {amp_params}")
            try:
                logger.info("Initializing Apex amp from: {}".format(amp.__path__))
            except NameError:
                # If apex/amp is available it will be imported above
                raise RuntimeError(
                    "Unable to import apex/amp, please make sure it is installed")
            self.module, self.optimizer = amp.initialize(self.module, basic_optimizer, **amp_params)
            self._broadcast_model()
        elif self.fp16_enabled():
            self.optimizer = self._configure_fp16_optimizer(basic_optimizer)
        else:
            self.optimizer = basic_optimizer
        logger.info('DeepSpeed Final Optimizer = {}'.format(self.optimizer))

    def _configure_basic_optimizer(self, model_parameters):
        optimizer_parameters = self.optimizer_params()
        # print(optimizer_parameters.keys())
        if 'max_grad_norm' in optimizer_parameters.keys():
            raise ValueError(
                "'max_grad_norm' is not supported as an optimizer parameter, please switch to using the deepspeed parameter 'gradient_clipping' see: https://www.deepspeed.ai/docs/config-json/#gradient-clipping for more details"
            )

        if self.optimizer_name() in [ADAM_OPTIMIZER, ADAMW_OPTIMIZER]:
            torch_adam = optimizer_parameters.pop(TORCH_ADAM_PARAM, False)
            adam_w_mode = self.optimizer_name() == ADAMW_OPTIMIZER
            # zero-offload  torch-adam  adam_w_mode optimizer
            # T|F           T           T           torch.optim.AdamW
            # T|F           T           F           torch.optim.Adam
            # T             F           T|F         DeepSpeedCPUAdam(adam_w_mode)
            # F             F           T|F         FusedAdam(adam_w_mode)
            if torch_adam:
                if adam_w_mode:
                    optimizer = torch.optim.AdamW(model_parameters,
                                                  **optimizer_parameters)
                else:
                    optimizer = torch.optim.Adam(model_parameters,
                                                 **optimizer_parameters)
            elif self.zero_cpu_offload():
                optimizer = DeepSpeedCPUAdam(model_parameters,
                                             **optimizer_parameters,
                                             adamw_mode=adam_w_mode)
            else:
                optimizer_parameters['adam_w_mode'] = adam_w_mode
                optimizer = FusedAdam(model_parameters, **optimizer_parameters)

        elif self.optimizer_name() == LAMB_OPTIMIZER:
            from deepspeed.ops.lamb import FusedLamb
            optimizer = FusedLamb(model_parameters, **optimizer_parameters)
        elif self.optimizer_name() == ONEBIT_ADAM_OPTIMIZER:
            from deepspeed.runtime.fp16.onebit_adam import OnebitAdam
            optimizer = OnebitAdam(model_parameters, self, **optimizer_parameters)
        else:
            torch_optimizer = getattr(torch.optim, self.optimizer_name())
            optimizer = torch_optimizer(model_parameters, **optimizer_parameters)
        return optimizer

    def _configure_fp16_optimizer(self, optimizer):
        initial_dynamic_scale = self.initial_dynamic_scale()
        dynamic_loss_args = self.dynamic_loss_scale_args()
        clip_grad = self.gradient_clipping()
        if isinstance(optimizer,
                      FusedAdam) or self.optimizer_name() == ONEBIT_ADAM_OPTIMIZER:
            if self.dynamic_loss_scale():
                logger.info('Creating fp16 optimizer with dynamic loss scale')
                timers = self.timers if self.wall_clock_breakdown() else None
                optimizer = FP16_Optimizer(
                    optimizer,
                    dynamic_loss_scale=True,
                    initial_dynamic_scale=initial_dynamic_scale,
                    dynamic_loss_args=dynamic_loss_args,
                    mpu=self.mpu,
                    clip_grad=clip_grad,
                    fused_adam_legacy=self.optimizer_legacy_fusion(),
                    timers=timers)
            else:
                logger.info('Creating fp16 optimizer with static loss scale: {}'.format(
                    self.loss_scale()))
                optimizer = FP16_Optimizer(
                    optimizer,
                    static_loss_scale=self.loss_scale(),
                    mpu=self.mpu,
                    clip_grad=clip_grad,
                    fused_adam_legacy=self.optimizer_legacy_fusion())
        else:
            logger.info('Creating fp16 unfused optimizer with dynamic loss scale')
            optimizer = FP16_UnfusedOptimizer(
                optimizer,
                static_loss_scale=self.loss_scale(),
                dynamic_loss_scale=self.dynamic_loss_scale(),
                dynamic_loss_args=dynamic_loss_args,
                mpu=self.mpu,
                clip_grad=clip_grad,
                fused_lamb_legacy=self.optimizer_name() == LAMB_OPTIMIZER)

        return optimizer

    def _configure_zero_optimizer(self, optimizer):
        zero_stage = self.zero_optimization_stage()
        logger.info('Creating fp16 ZeRO stage {} optimizer'.format(zero_stage))
        assert not self.allreduce_always_fp32(), "ZeRO does not support 'fp32_allreduce': true"
        if zero_stage == ZERO_OPTIMIZATION_OPTIMIZER_STATES:
            assert self.zero_reduce_scatter(), 'Stage 1 only supports reduce scatter mode'
            optimizer = FP16_DeepSpeedZeroOptimizer_Stage1(
                optimizer,
                static_loss_scale=self.loss_scale(),
                dynamic_loss_scale=self.dynamic_loss_scale(),
                dynamic_loss_args=self.dynamic_loss_scale_args(),
                clip_grad=self.gradient_clipping(),
                all_gather_partitions=self.zero_allgather_partitions(),
                allgather_size=self.zero_allgather_bucket_size(),
                max_elements_per_comm=self.zero_reduce_bucket_size(),
                dp_process_group=self.data_parallel_group,
                elastic_checkpoint=self.zero_elastic_checkpoint(),
                mpu=self.mpu)
        elif zero_stage == ZERO_OPTIMIZATION_GRADIENTS:
            optimizer = FP16_DeepSpeedZeroOptimizer(
                optimizer,
                timers=self.timers,
                static_loss_scale=self.loss_scale(),
                dynamic_loss_scale=self.dynamic_loss_scale(),
                dynamic_loss_args=self.dynamic_loss_scale_args(),
                clip_grad=self.gradient_clipping(),
                contiguous_gradients=self.zero_contiguous_gradients(),
                reduce_bucket_size=self.zero_reduce_bucket_size(),
                allgather_bucket_size=self.zero_allgather_bucket_size(),
                dp_process_group=self.data_parallel_group,
                reduce_scatter=self.zero_reduce_scatter(),
                overlap_comm=self.zero_overlap_comm(),
                cpu_offload=self.zero_cpu_offload(),
                mpu=self.mpu,
                postscale_gradients=self.postscale_gradients(),
                gradient_predivide_factor=self.gradient_predivide_factor(),
                gradient_accumulation_steps=self.gradient_accumulation_steps())
        else:
            raise NotImplementedError("ZeRO stage {} not implemented".format(zero_stage))

        return optimizer

    def _configure_progressive_layer_drop(self):
        pld = ProgressiveLayerDrop(theta=self.pld_theta(), gamma=self.pld_gamma())

        return pld

    def deepspeed_io(self,
                     dataset,
                     batch_size=None,
                     route=ROUTE_TRAIN,
                     pin_memory=True,
                     data_sampler=None,
                     collate_fn=None,
                     num_local_io_workers=None):
        if not isinstance(dataset, torch.utils.data.Dataset):
            raise ValueError("Training data must be a torch Dataset")

        if data_sampler is None and (route == ROUTE_PREDICT or route == ROUTE_EVAL):
            data_sampler = torch.utils.data.SequentialSampler(dataset)

        if batch_size is None:
            batch_size = self.train_micro_batch_size_per_gpu()

        if collate_fn is None:
            collate_fn = self.collate_fn

        # Currently we only use timer in train route
        deepspeed_io_timer = None
        if route == ROUTE_TRAIN:
            deepspeed_io_timer = self.tput_timer

        # If mpu is provied, forward world size and parallel rank to sampler.
        data_parallel_world_size = None
        data_parallel_rank = None
        if self.mpu is not None:
            data_parallel_world_size = self.mpu.get_data_parallel_world_size()
            data_parallel_rank = self.mpu.get_data_parallel_rank()

        return DeepSpeedDataLoader(dataset=dataset,
                                   batch_size=batch_size,
                                   pin_memory=pin_memory,
                                   collate_fn=collate_fn,
                                   local_rank=self.local_rank,
                                   tput_timer=deepspeed_io_timer,
                                   num_local_io_workers=num_local_io_workers,
                                   data_sampler=data_sampler,
                                   data_parallel_world_size=data_parallel_world_size,
                                   data_parallel_rank=data_parallel_rank)

    def train(self, mode=True):
        r"""
        """

        self.warn_unscaled_loss = True
        self.module.train(mode)

    def eval(self):
        r"""
        """

        self.warn_unscaled_loss = True
        self.module.train(False)

    def _scale_loss(self, prescaled_loss):
        if isinstance(prescaled_loss, torch.Tensor):
            scaled_loss = prescaled_loss / self.gradient_accumulation_steps()
        elif isinstance(prescaled_loss, tuple) or isinstance(prescaled_loss, list):
            scaled_loss = []
            for l in prescaled_loss:
                if isinstance(l, torch.Tensor):
                    scaled_loss.append(l / self.gradient_accumulation_steps())
                else:
                    scaled_loss.append(l)
        else:
            scaled_loss = prescaled_loss
            if self.warn_unscaled_loss:
                logger.warning(
                    f'DeepSpeed unable to scale loss because of type: {type(prescaled_loss)}'
                )
                self.warn_unscaled_loss = False

        return scaled_loss

    def forward(self, *inputs, **kwargs):
        r"""Execute forward propagation

        Arguments:
            *inputs: Variable length input list
            **kwargs: variable length keyword arguments
        """
        if self.flops_profiler_enabled(
        ) and self.global_steps == self.flops_profiler_start_step(
        ) and self.global_rank == 0:
            self.flops_profiler = FlopsProfiler(self.module)
            self.flops_profiler.start_profile(ignore_list=None)

        if self.flops_profiler_enabled(
        ) and self.global_steps == self.flops_profiler_end_step(
        ) and self.global_rank == 0:
            print('{:<30}  {:<8}'.format(
                'Number of multiply-adds: ',
                self.flops_profiler.get_total_flops(in_str=False)))
            print('{:<30}  {:<8}'.format(
                'Number of parameters: ',
                self.flops_profiler.get_total_params(in_str=False)))
            print('{:<30}  {:<8}'.format('Number of steps profiled: ',
                                         self.flops_profiler.get_total_steps()))
            self.flops_profiler.print_model_profile()
            self.flops_profiler.print_model_aggregated_profile(
                module_depth=self.flops_profiler_module_depth(),
                top_modules=self.flops_profiler_top_modules())
            self.flops_profiler.flops = self.flops_profiler.get_total_flops()
            self.flops_profiler.params = self.flops_profiler.get_total_params()
            self.flops_profiler.end_profile()

        if self.module.training and self.progressive_layer_drop:
            kwargs.update(self.progressive_layer_drop.get_state())

        if self.wall_clock_breakdown():
            self.timers('forward_microstep').start()
            self.timers('forward').start()

        if self.training_dataloader is None:
            self.tput_timer.start()
        loss = self.module(*inputs, **kwargs)

        if self.wall_clock_breakdown():
            self.timers('forward').stop()
            self.timers('forward_microstep').stop()

        return loss

    def allreduce_gradients(self, bucket_size=MEMORY_OPT_ALLREDUCE_SIZE):
        #Zero stage 2 communicates during non gradient accumulation boundaries as well
        if self.zero_optimization_partition_gradients():
            self.optimizer.overlapping_partition_gradients_reduce_epilogue()

        #Communicate only at gradient accumulation boundaries
        elif self.is_gradient_accumulation_boundary():
            if self.zero_optimization_stage() == ZERO_OPTIMIZATION_OPTIMIZER_STATES:
                assert self.zero_reduce_scatter()
                self.optimizer.reduce_scatter_gradients(
                    postscale_gradients=self.postscale_gradients(),
                    gradient_predivide_factor=self.gradient_predivide_factor(),
                    gradient_average=self.gradient_average)
            else:
                self.buffered_allreduce_fallback(elements_per_buffer=bucket_size)

    def backward(self, loss, allreduce_gradients=True, release_loss=False):
        r"""Execute backward pass on the loss

        Arguments:
            loss: Torch tensor on which to execute backward propagation
            allreduce_gradients: If this is False, then gradient averaging will be skipped. Default is True.
        """

        if not allreduce_gradients:
            logger.warning(
                f'Argument `allreduce_gradients` is deprecated, ignored, and will soon be removed'
            )

        # scale loss w.r.t. gradient accumulation if needed
        if self.gradient_accumulation_steps() > 1:
            loss = self._scale_loss(loss.float())

        # Log training Loss
        if self.tensorboard_enabled():
            if self.is_gradient_accumulation_boundary():
                if self.global_rank == 0:
                    self.summary_events = [
                        (f'Train/Samples/train_loss',
                         loss.mean().item() * self.gradient_accumulation_steps(),
                         self.global_samples)
                    ]
                    for event in self.summary_events:  # write_summary_events
                        self.summary_writer.add_scalar(event[0], event[1], event[2])
                    self.summary_writer.flush()

        if self.wall_clock_breakdown():
            self.timers('backward_microstep').start()
            self.timers('backward').start()

        assert self.optimizer is not None, "must provide optimizer during " \
                                           "init in order to use backward"

        if self.wall_clock_breakdown():
            self.timers('backward_inner_microstep').start()
            self.timers('backward_inner').start()

        if self.zero_optimization():
            self.optimizer.is_gradient_accumulation_boundary = self.is_gradient_accumulation_boundary(
            )
            self.optimizer.backward(loss)
        elif self.amp_enabled():
            # AMP requires delaying unscale when inside gradient accumulation boundaries
            # https://nvidia.github.io/apex/advanced.html#gradient-accumulation-across-iterations
            delay_unscale = not self.is_gradient_accumulation_boundary()
            with amp.scale_loss(loss,
                                self.optimizer,
                                delay_unscale=delay_unscale) as scaled_loss:
                scaled_loss.backward()
        elif self.fp16_enabled():
            self.optimizer.backward(loss)
        else:
            loss.backward()

        if self.wall_clock_breakdown():
            self.timers('backward_inner').stop()
            self.timers('backward_inner_microstep').stop()

        if self.wall_clock_breakdown():
            self.timers('backward_allreduce_microstep').start()
            self.timers('backward_allreduce').start()

        if self.enable_backward_allreduce:
            self.allreduce_gradients()

        if self.wall_clock_breakdown():
            self.timers('backward_allreduce').stop()
            self.timers('backward_allreduce_microstep').stop()
            self.timers('backward').stop()
            self.timers('backward_microstep').stop()

        if release_loss:
            # loss.data = None
            pass

        return loss

    def is_gradient_accumulation_boundary(self):
        """Query whether the current micro-batch is at the boundary of
        gradient accumulation, and thus will trigger gradient reductions and
        an optimizer step.

        Returns:
            bool: if the current step is a gradient accumulation boundary.
        """
        return (self.micro_steps + 1) % \
            self.gradient_accumulation_steps() == 0

    def zero_grad(self):
        """
        Zero parameter grads.
        """
        for param_name, param in self.module.named_parameters():
            param.grad = None

    def clip_fp32_gradients(self):
        torch.nn.utils.clip_grad_norm_(parameters=self.module.parameters(),
                                       max_norm=self.gradient_clipping())

    def _take_model_step(self, lr_kwargs):
        if self.gradient_clipping() > 0.0:
            if not self.fp16_enabled() and not self.amp_enabled():
                self.clip_fp32_gradients()
            elif self.amp_enabled():
                # AMP's recommended way of doing clipping
                # https://nvidia.github.io/apex/advanced.html#gradient-clipping
                master_params = amp.master_params(self.optimizer)
                torch.nn.utils.clip_grad_norm_(parameters=master_params,
                                               max_norm=self.gradient_clipping())
        self.optimizer.step()

        #zero grad in basic optimizer could be unreliable and may not exhibit
        #the behaviour that we want
        if not self.zero_optimization() and not self.fp16_enabled(
        ) and not self.amp_enabled():
            self.zero_grad()
        else:
            self.optimizer.zero_grad()

        report_progress = self.global_rank == 0 if self.global_rank else True

        # Check overlow here since in DS fp16 optimizer, the overflow is updated in above step() function.
        overflow = False
        if hasattr(self.optimizer, 'overflow'):
            overflow = self.optimizer.overflow

        if overflow:
            self.skipped_steps += 1
        else:
            if self.lr_scheduler is not None:
                self.lr_scheduler.step(**(lr_kwargs or {}))
            if report_progress and (self.global_steps + 1) % self.steps_per_print() == 0:
                self._report_progress(self.global_steps + 1)

        self.global_steps += 1
        self.global_samples += self.train_batch_size()

    def step(self, lr_kwargs=None):
        r"""Execute the weight update step after forward and backward propagation
        on effective_train_batch.
        """
        if self.wall_clock_breakdown():
            self.timers('step_microstep').start()
            self.timers('step').start()

        assert self.optimizer is not None, "must provide optimizer during " \
                                           "init in order to use step"
        report_progress = self.global_rank == 0 if self.global_rank else True

        # Update the model when we reach gradient accumulation boundaries
        if self.is_gradient_accumulation_boundary():
            if self.progressive_layer_drop:
                self.progressive_layer_drop.update_state(self.global_steps)

            self._take_model_step(lr_kwargs)

        self.tput_timer.stop(report_progress)

        # Log learning rate
        if self.tensorboard_enabled():
            if self.is_gradient_accumulation_boundary():
                if self.global_rank == 0:
                    self.summary_events = [(f'Train/Samples/lr',
                                            self.get_lr()[0],
                                            self.global_samples)]
                    for event in self.summary_events:  # write_summary_events
                        self.summary_writer.add_scalar(event[0], event[1], event[2])
                    if self.fp16_enabled() and hasattr(self.optimizer, 'cur_scale'):
                        self.summary_events.append((f'Train/Samples/loss_scale',
                                                    self.optimizer.cur_scale,
                                                    self.global_samples))
                    for event in self.summary_events:  # write_summary_events
                        self.summary_writer.add_scalar(event[0], event[1], event[2])
                    self.summary_writer.flush()

        if self.wall_clock_breakdown():
            self.timers('step').stop()
            self.timers('step_microstep').stop()
            timer_names = [
                'forward_microstep',
                'backward_microstep',
                'backward_inner_microstep',
                'backward_allreduce_microstep',
                'step_microstep'
            ]
            self.timers.log(names=timer_names, memory_breakdown=self.memory_breakdown())

            # Log timing
            if self.is_gradient_accumulation_boundary():
                if self.tensorboard_enabled():
                    if self.global_rank == 0:
                        self.summary_events = [
                            (f'Train/Samples/elapsed_time_ms_forward',
                             self.timers('forward').elapsed(reset=False) * 1000.0,
                             self.global_samples),
                            (f'Train/Samples/elapsed_time_ms_backward',
                             self.timers('backward').elapsed(reset=False) * 1000.0,
                             self.global_samples),
                            (f'Train/Samples/elapsed_time_ms_backward_inner',
                             self.timers('backward_inner').elapsed(reset=False) * 1000.0,
                             self.global_samples),
                            (f'Train/Samples/elapsed_time_ms_backward_allreduce',
                             self.timers('backward_allreduce').elapsed(reset=False) *
                             1000.0,
                             self.global_samples),
                            (f'Train/Samples/elapsed_time_ms_step',
                             self.timers('step').elapsed(reset=False) * 1000.0,
                             self.global_samples)
                        ]
                        for event in self.summary_events:  # write_summary_events
                            self.summary_writer.add_scalar(event[0], event[1], event[2])
                        self.summary_writer.flush()

            if self.wall_clock_breakdown():
                self.timers.log([
                    'forward',
                    'backward',
                    'backward_inner',
                    'backward_allreduce',
                    'step'
                ])

        self.micro_steps += 1

    def _get_optimizer_param(self, param_name):
        result = []
        if not self.optimizer:
            return result
        for group in self.optimizer.param_groups:
            if param_name in group:
                result.append(group[param_name])
            else:
                result.append(0.0)
        return result

    def get_lr(self):
        return self._get_optimizer_param('lr')

    def get_type(self):
        return self._get_optimizer_param('type')

    def get_mom(self):
        if self.optimizer_name() in ['SGD', 'RMSprop']:
            return self._get_optimizer_param('momentum')
        else:
            return self._get_optimizer_param('betas')

    def get_pld_theta(self):
        if self.progressive_layer_drop:
            return self.progressive_layer_drop.get_theta()
        else:
            return None

    def _report_progress(self, step):
        lr = self.get_lr()
        mom = self.get_mom()
        log_dist(f'step={step}, skipped={self.skipped_steps}, lr={lr}, mom={mom}',
                 ranks=[0])

    def allreduce_bucket(self, bucket):
        tensor = self.flatten(bucket)

        tensor_to_allreduce = tensor

        if self.allreduce_always_fp32():
            tensor_to_allreduce = tensor.float()

        if self.postscale_gradients():
            if self.gradient_predivide_factor() != 1.0:
                tensor_to_allreduce.mul_(1. / self.gradient_predivide_factor())

            dist.all_reduce(tensor_to_allreduce, group=self.data_parallel_group)

            if self.gradient_average:
                if self.gradient_predivide_factor() != self.dp_world_size:
                    tensor_to_allreduce.mul_(self.gradient_predivide_factor() /
                                             self.dp_world_size)
        else:
            tensor_to_allreduce.div_(self.dp_world_size)
            dist.all_reduce(tensor_to_allreduce, group=self.data_parallel_group)

        if self.allreduce_always_fp32() and tensor is not tensor_to_allreduce:
            tensor.copy_(tensor_to_allreduce)

        return tensor

    def allreduce_and_copy(self, small_bucket):
        allreduced = self.allreduce_bucket(small_bucket)
        for buf, synced in zip(small_bucket, self.unflatten(allreduced, small_bucket)):
            buf.copy_(synced)

    def allreduce_no_retain(self, bucket, numel_per_bucket=500000000):
        small_bucket = []
        numel = 0
        for tensor in bucket:
            small_bucket.append(tensor)
            numel = numel + tensor.numel()
            if numel > numel_per_bucket:
                self.allreduce_and_copy(small_bucket)
                small_bucket = []
                numel = 0
        if len(small_bucket) > 0:
            self.allreduce_and_copy(small_bucket)

    def buffered_allreduce_fallback(self, grads=None, elements_per_buffer=500000000):
        grads = []
        for param_name, param in self.module.named_parameters():
            if param.grad is None:
                # In cases where there is an imbalance of empty grads across
                # ranks we must create empty grads, this will ensure that every
                # rank is reducing the same size. In some cases it may make
                # sense in the future to support the ability to average not
                # w.r.t. world size but with a different value.
                param.grad = torch.zeros(param.size(),
                                         dtype=param.dtype,
                                         device=param.device)
                grads.append(param.grad.data)
            else:
                grad_data = param.grad.data
                if self.sparse_gradients_enabled(
                ) and param_name in self.csr_tensor_module_names:
                    grads.append(CSRTensor(grad_data))
                else:
                    grads.append(grad_data)

        split_buckets = split_half_float_double_csr(grads)

        for i, bucket_tuple in enumerate(split_buckets):
            bucket_type, bucket = bucket_tuple
            if bucket_type == CSRTensor.type():
                self.csr_allreduce_no_retain(bucket)
            else:
                self.allreduce_no_retain(bucket, numel_per_bucket=elements_per_buffer)

    def csr_allreduce_no_retain(self, bucket):
        allreduced_csrs = self.csr_allreduce_bucket(bucket)
        # Densify csr tensor and copy back to original location
        for csr in allreduced_csrs:
            dense_tensor = csr.to_dense()
            csr.orig_dense_tensor.copy_(dense_tensor)

    def csr_allreduce_bucket(self, bucket):
        csr_list = []
        for csr in bucket:
            csr_list.append(self.csr_allreduce(csr))
        return csr_list

    def csr_allreduce(self, csr):
        # Pre-divide for fp16 stability
        csr.values.div_(self.dp_world_size)

        indices_device_list = self.csr_all_gather(csr.indices)
        values_device_list = self.csr_all_gather(csr.values)

        csr.indices = torch.cat(indices_device_list)
        csr.values = torch.cat(values_device_list)
        return csr

    def csr_all_gather(self, value):
        my_size = torch.LongTensor([value.size()[0]]).to(self.device)
        all_sizes = self.all_gather_scalar(my_size)
        max_size = torch.cat(all_sizes).max()
        fill_size = (max_size - my_size)

        assert value.dim() in [1, 2]
        if value.dim() == 1:
            if fill_size > 0:
                value = torch.cat([value, value.new_zeros(fill_size)])
            tensor_list = [value.new_zeros(max_size) for _ in range(self.dp_world_size)]
        else:
            if fill_size > 0:
                value = torch.cat([value, value.new_zeros(fill_size, value.size()[1])])
            tensor_list = [
                value.new_zeros(max_size,
                                value.size()[1]) for _ in range(self.dp_world_size)
            ]

        dist.all_gather(tensor_list, value, group=self.data_parallel_group)
        tensors = []
        for dev_idx, t in enumerate(tensor_list):
            size = all_sizes[dev_idx][0]
            tensors.append(
                t.index_select(0,
                               torch.LongTensor(range(size)).to(self.device)))

        return tensors

    def all_gather_scalar(self, value):
        tensor_list = [value.new_zeros(value.size()) for _ in range(self.dp_world_size)]
        dist.all_gather(tensor_list, value, group=self.data_parallel_group)
        return tensor_list

    def module_state_dict(self, destination=None, prefix='', keep_vars=False):
        sd = self.module.state_dict(destination, prefix, keep_vars)
        return sd

    def load_module_state_dict(self, state_dict, strict=True):
        self.module.load_state_dict(state_dict, strict=strict)

    def _get_rank_zero_ckpt_name(self, checkpoints_path, tag, mp_rank, dp_rank):
        filename = 'zero_pp_rank_{}'.format(dp_rank)
        zero_ckpt_name = os.path.join(
            checkpoints_path,
            str(tag),
            filename + '_mp_rank_{:02d}'.format(mp_rank) + 'optim_states.pt')
        return zero_ckpt_name

    def _get_zero_ckpt_name(self, checkpoints_path, tag):
        mp_rank = 0 if self.mpu is None else self.mpu.get_model_parallel_rank()
        pp_rank = torch.distributed.get_rank(group=self.optimizer.dp_process_group)
        return self._get_rank_zero_ckpt_name(checkpoints_path, tag, mp_rank, pp_rank)

    def _get_ckpt_name(self, checkpoints_path, tag):
        mp_rank = 0 if self.mpu is None else self.mpu.get_model_parallel_rank()
        ckpt_name = os.path.join(checkpoints_path,
                                 str(tag),
                                 'mp_rank_{:02d}'.format(mp_rank) + '_model_states.pt')
        return ckpt_name

    def load_checkpoint(self,
                        load_dir,
                        tag=None,
                        load_module_strict=True,
                        load_optimizer_states=True,
                        load_lr_scheduler_states=True):
        """Load training checkpoint

        Arguments:
            load_dir: Required. Directory to load the checkpoint from
            tag: Checkpoint tag used as a unique identifier for checkpoint, if not provided will attempt to load tag in 'latest' file
            load_module_strict: Optional. Boolean to strictly enforce that the keys in state_dict of module and checkpoint match.
            load_optimizer_states: Optional. Boolean to load the training optimizer states from Checkpoint. Ex. ADAM's momentum and variance
            load_lr_scheduler_states: Optional. Boolean to add the learning rate scheduler states from Checkpoint.
        Returns:
            A tuple of ``load_path`` and ``client_state``.

            *``load_path``: Path of the loaded checkpoint. ``None`` if loading the checkpoint failed.

            *``client_state``: State dictionary used for loading required training states in the client code.
        """

        if tag is None:
            latest_path = os.path.join(load_dir, 'latest')
            if os.path.isfile(latest_path):
                with open(latest_path, 'r') as fd:
                    tag = fd.read().strip()
            else:
                logger.warning(f"Unable to find latest file at {latest_path}, if trying to load latest " \
                "checkpoint please ensure this file exists or pass an explicit checkpoint tag when loading a checkpoint.")
                return None, None

        load_path, client_states = self._load_checkpoint(load_dir,
                                                         tag,
                                                         load_module_strict=load_module_strict,
                                                         load_optimizer_states=load_optimizer_states,
                                                         load_lr_scheduler_states=load_lr_scheduler_states)

        if self.zero_optimization() and load_path is not None:
            self._load_zero_checkpoint(load_dir,
                                       tag,
                                       load_optimizer_states=load_optimizer_states)

        return load_path, client_states

    def _load_checkpoint(self,
                         load_dir,
                         tag,
                         load_module_strict=True,
                         load_optimizer_states=True,
                         load_lr_scheduler_states=True):

        load_path = self._get_ckpt_name(load_dir, tag)

        if not os.path.exists(load_path):
            logger.warn(
                'Client provided checkpoint load path: {} does not exist ... skip checkpoint load'
                .format(load_path))
            return None, None

        logger.info(f'rank: {self.global_rank} loading checkpoint: {load_path}')
        checkpoint = torch.load(load_path, map_location=lambda storage, loc: storage)

        if isinstance(self.module, PipelineModule):
            # Pipeline parallelism uses this to load its own checkpoint files.
            self._curr_ckpt_path = os.path.join(load_dir, tag)

        self.load_module_state_dict(state_dict=checkpoint['module'],
                                    strict=load_module_strict)
        if self.optimizer is not None and not self.zero_optimization():
            if self.fp16_enabled():
                self.optimizer.load_state_dict(
                    checkpoint['optimizer'],
                    load_optimizer_states=load_optimizer_states)
            elif load_optimizer_states:
                self.optimizer.load_state_dict(checkpoint['optimizer'])

        if load_lr_scheduler_states and self.lr_scheduler is not None:
            self.lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])

        self.csr_tensor_module_names = checkpoint['csr_tensor_module_names']
        self.global_steps = checkpoint['global_steps']
        self.global_samples = checkpoint.get('global_samples',
                                             self.global_steps * self.train_batch_size())
        self.skipped_steps = checkpoint['skipped_steps']
        self.loaded_checkpoint_mp_world_size = checkpoint['mp_world_size']
        self.loaded_checkpoint_dp_world_size = checkpoint['dp_world_size']
        deepspeed_states = [
            'module',
            'optimizer',
            'lr_scheduler',
            'csr_tensor_module_names',
            'skipped_steps',
            'global_steps',
            'dp_world_size',
            'mp_world_size'
        ]
        client_state = {
            key: value
            for key,
            value in checkpoint.items() if not key in deepspeed_states
        }

        return load_path, client_state

    def _load_zero_checkpoint(self, load_dir, tag, load_optimizer_states=True):
        zero_sd_list = self._get_all_zero_checkpoints(load_dir, tag)
        if zero_sd_list is None:
            return

        self.optimizer.load_state_dict(
            state_dict_list=zero_sd_list,
            load_optimizer_states=load_optimizer_states,
            load_from_fp32_weights=self.zero_load_from_fp32_weights())
        print(
            f'loading {len(zero_sd_list)} zero partition checkpoints for rank {self.global_rank}'
        )

    def _get_mp_rank_zero_checkpoint_names(self, load_dir, tag, mp_rank, dp_world_size):
        zero_ckpt_names = []
        for dp_rank in range(dp_world_size):
            ckpt_name = self._get_rank_zero_ckpt_name(checkpoints_path=load_dir,
                                                      tag=tag,
                                                      mp_rank=mp_rank,
                                                      dp_rank=dp_rank)
            zero_ckpt_names.append(ckpt_name)

        return zero_ckpt_names

    def _get_all_zero_checkpoint_names(self,
                                       load_dir,
                                       tag,
                                       mp_world_size,
                                       dp_world_size):
        zero_ckpt_names = []
        for mp_rank in range(mp_world_size):
            mp_rank_ckpt_names = self._get_mp_rank_zero_checkpoint_names(
                load_dir=load_dir,
                tag=tag,
                mp_rank=mp_rank,
                dp_world_size=dp_world_size)
            zero_ckpt_names += mp_rank_ckpt_names

        return zero_ckpt_names

    def _get_all_zero_checkpoints(self, load_dir, tag):
        mp_rank = 0 if self.mpu is None else self.mpu.get_model_parallel_rank()
        zero_ckpt_names = self._get_mp_rank_zero_checkpoint_names(
            load_dir=load_dir,
            tag=tag,
            mp_rank=mp_rank,
            dp_world_size=self.loaded_checkpoint_dp_world_size)
        invalid_zero_ckpt_paths = []
        for ckpt_name in zero_ckpt_names:
            if not os.path.exists(ckpt_name):
                invalid_zero_ckpt_paths.append(ckpt_name)

        if len(invalid_zero_ckpt_paths) > 0:
            logger.warn(
                f"Client provided zero checkpoint load paths: {invalid_zero_ckpt_paths} does not exist"
            )
            return None

        zero_sd_list = []
        for ckpt_name in zero_ckpt_names:
            zero_sd_list.append(torch.load(ckpt_name, map_location='cpu'))

        zero_optimizer_sd = [sd['optimizer_state_dict'] for sd in zero_sd_list]
        print(
            f"successfully loaded {len(zero_optimizer_sd)} ZeRO state_dicts for rank {self.global_rank}"
        )
        return zero_optimizer_sd

    def _checkpoint_tag_validation(self, tag):
        if self.checkpoint_tag_validation_enabled():
            s_hash = hashlib.sha1(tag.encode())
            bhash = torch.ByteTensor([s_hash.digest()]).flatten().to(self.device)
            max_bhash = bhash.clone()
            min_bhash = bhash.clone()
            dist.all_reduce(max_bhash, op=torch.distributed.ReduceOp.MAX)
            dist.all_reduce(min_bhash, op=torch.distributed.ReduceOp.MIN)
            valid = all(min_bhash == bhash) and all(max_bhash == bhash)
            msg = f"[rank={dist.get_rank()}] The checkpoint tag name '{tag}' is not consistent across " \
                "all ranks. Including rank unique information in checkpoint tag could cause issues when " \
                "restoring with different world sizes."
            if self.checkpoint_tag_validation_fail():
                assert valid, msg
            elif not valid:
                logger.warning(msg)

    def save_checkpoint(self, save_dir, tag=None, client_state={}, save_latest=True):
        r"""Save training checkpoint

        Arguments:
            save_dir: Required. Directory for saving the checkpoint
            tag: Optional. Checkpoint tag used as a unique identifier for the checkpoint, global step is
                used if not provided. Tag name must be the same across all ranks.
            client_state: Optional. State dictionary used for saving required training states in the client code.
            save_latest: Optional. Save a file 'latest' pointing to the latest saved checkpoint.
        """

        # This is to make sure the checkpoint names are created without collision
        # There seems to be issue creating them in parallel

        # Ensure save_dir directory exists
        os.makedirs(save_dir, exist_ok=True)

        if tag is None:
            tag = f"global_step{self.global_steps}"

        # Ensure checkpoint tag is consistent across ranks
        self._checkpoint_tag_validation(tag)

        if self.save_non_zero_checkpoint:
            self._create_checkpoint_file(save_dir, tag, False)
            self._save_checkpoint(save_dir, tag, client_state=client_state)

        if self.save_zero_checkpoint:
            self._create_zero_checkpoint_files(save_dir, tag)
            self._save_zero_checkpoint(save_dir, tag)

        # Save latest checkpoint tag
        if save_latest:
            with open(os.path.join(save_dir, 'latest'), 'w') as fd:
                fd.write(tag)

        return True

    def _create_checkpoint_file(self, save_dir, tag, zero_checkpoint):
        name_function = self._get_zero_ckpt_name if zero_checkpoint else self._get_ckpt_name
        try:
            checkpoint_name = name_function(save_dir, tag)
            ensure_directory_exists(checkpoint_name)
        except:
            logger.error(f'Failed saving model checkpoint to {save_dir} with tag {tag}')
            return False

        return True

    def _create_zero_checkpoint_files(self, save_dir, tag):
        success = True
        # zero checkpoint files are created sequentially
        for rank in range(self.world_size):
            if rank == self.global_rank:
                success = self._create_checkpoint_file(save_dir, tag, True)

            dist.barrier()

        return success

    def _save_checkpoint(self, save_dir, tag, client_state={}):

        save_path = self._get_ckpt_name(save_dir, tag)
        # A hack to save the checkpointing directory. Pipeline parallelism overrides
        # module_state_dict() and uses this path to save the model. module_state_dict()
        # then instead just returns None.
        self._curr_ckpt_path = os.path.join(save_dir, tag)

        state = {
            'module':
            self.module_state_dict(),
            'optimizer':
            self.optimizer.state_dict()
            if self.optimizer and not self.zero_optimization() else None,
            'lr_scheduler':
            self.lr_scheduler.state_dict() if self.lr_scheduler is not None else None,
            'csr_tensor_module_names':
            self.csr_tensor_module_names,
            'skipped_steps':
            self.skipped_steps,
            'global_steps':
            self.global_steps,
            'global_samples':
            self.global_samples,
            'dp_world_size':
            self.dp_world_size,
            'mp_world_size':
            self.mp_world_size
        }
        state.update(client_state)

        log_dist(message=f'Saving model checkpoint: {save_path}', ranks=[0])
        #logger.info('Saving model checkpoint: {}'.format(save_path))
        torch.save(state, save_path)
        self._curr_save_path = None

    def _save_zero_checkpoint(self, save_path, tag):
        zero_checkpoint_name = self._get_zero_ckpt_name(save_path, tag)
        zero_sd = {'optimizer_state_dict': self.optimizer.state_dict()}
        torch.save(zero_sd, zero_checkpoint_name)
        logger.info('zero checkpoint saved {}'.format(zero_checkpoint_name))
