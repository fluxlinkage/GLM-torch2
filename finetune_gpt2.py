import os
import sys
from arguments import get_args

# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Finetune utilities."""

import torch
from configure_data import prepare_tokenizer

from utils import print_rank_0
from utils import Timers
import mpu
from pretrain_gpt2 import setup_model_and_optimizer
from utils import load_checkpoint, save_checkpoint
from pretrain_gpt2 import report_iteration_metrics
from pretrain_gpt2 import evaluate_and_print_results
from pretrain_gpt2 import train_step
from pretrain_gpt2 import initialize_distributed
from pretrain_gpt2 import set_random_seed
from model import PyTorchDistributedDataParallel as TorchDDP
from model import DistributedDataParallel as LocalDDP
from fp16 import FP16_Module


def process_batch(batch, args):
    """Process batch and produce inputs for the model."""
    tokens = batch['text'].long().cuda().contiguous()
    types = batch['types'].long().cuda().contiguous()
    labels = batch['label'].long().cuda().contiguous()
    attention_mask = batch['padding_mask'].float().cuda().contiguous()
    if args.fp16:
        attention_mask = attention_mask.half()
    position_ids = torch.arange(tokens.size(-1), dtype=torch.long, device=tokens.device)
    block_position_ids = tokens.new_zeros(tokens.size(-1)).unsqueeze(0).unsqueeze(0).expand_as(tokens)
    position_ids = position_ids.unsqueeze(0).unsqueeze(0).expand_as(tokens)
    position_ids = torch.stack((position_ids, block_position_ids), dim=2)

    return tokens, types, labels, position_ids, attention_mask


def cross_entropy_forward_step(batch, model, args, timers, mems):
    """Simple forward step with cross-entropy loss."""
    # Get the batch.
    timers('batch generator').start()
    try:
        batch_ = next(batch)
    except BaseException:
        batch_ = batch
    tokens, types, labels, position_ids, attention_mask = process_batch(batch_, args)
    timers('batch generator').stop()

    # Forward model.
    logits, *mems = model(tokens, position_ids, attention_mask)

    # Cross-entropy loss.
    loss_func = torch.nn.CrossEntropyLoss()
    loss = loss_func(logits.contiguous().float(), labels)

    # Reduce loss for logging.

    return loss, mems, 'bert'


def build_data_loader(dataset, batch_size, num_workers, drop_last):
    """Data loader. Note that batch-size is the local (per GPU) batch-size."""

    # Sampler.
    world_size = mpu.get_data_parallel_world_size()
    rank = mpu.get_data_parallel_rank()
    sampler = torch.utils.data.distributed.DistributedSampler(
        dataset, num_replicas=world_size, rank=rank)

    # Data loader. Note that batch size is the per GPU batch size.
    data_loader = torch.utils.data.DataLoader(dataset,
                                              batch_size=batch_size,
                                              sampler=sampler,
                                              shuffle=False,
                                              num_workers=num_workers,
                                              drop_last=drop_last,
                                              pin_memory=True)

    return data_loader


def _build_infinite_size_dataloader(dataloader):
    """Build a looped dataloader with infinite size."""

    iterator = dataloader.__iter__()
    while True:
        try:
            yield iterator.__next__()
        except StopIteration:
            iterator = dataloader.__iter__()


def _build_train_valid_dataloaders(train_dataset, valid_dataset, args):
    """Traing and validation dataloaders."""
    print_rank_0('building train and validation dataloaders ...')
    # Training dataset.
    train_dataloader = build_data_loader(train_dataset, args.batch_size, args.num_workers, False)
    # Set the training iterations.
    args.train_iters_per_epoch = len(train_dataloader)
    args.train_iters = args.epochs * args.train_iters_per_epoch
    # Validation dataset. For this dataset, we do not need to set up
    # shuffling so we can just use a simple infinite loop.
    valid_dataloader_ = build_data_loader(valid_dataset, args.batch_size,
                                          args.num_workers, False)
    valid_dataloader = _build_infinite_size_dataloader(valid_dataloader_)

    return train_dataloader, valid_dataloader


def _train(model, optimizer, lr_scheduler, forward_step,
           train_dataloader, valid_dataloader, end_of_epoch_callback, args, timers):
    """Train the model."""

    # Turn on training mode which enables dropout.
    model.train()

    # Tracking loss.
    args.iteration = 0
    total_lm_loss = 0.0
    # Starting epoch and iteration
    start_epoch = args.iteration // args.train_iters_per_epoch
    start_iteration = args.iteration % args.train_iters_per_epoch

    # For each remaining epoch
    timers('interval time').start()
    for epoch in range(start_epoch, args.epochs):
        print_rank_0('working on epoch {} ...'.format(epoch + 1))

        # Set the data loader epoch to shuffle the index iterator.
        train_dataloader.sampler.set_epoch(args.seed + epoch)

        # For all the batches in the dataset.
        for iteration_, batch in enumerate(train_dataloader):

            # Ignore the iterations before starting value
            if iteration_ < start_iteration:
                continue
            # Set to zero so the next epoch does not skip any batches.
            start_iteration = 0

            # Train for one step.
            lm_loss, skipped_iter, _ = train_step(batch, model, optimizer, lr_scheduler, args, timers,
                                                  forward_step_func=forward_step)
            args.iteration += 1
            total_lm_loss += lm_loss.data.detach().float()

            # Logging.
            if args.iteration % args.log_interval == 0:
                learning_rate = optimizer.param_groups[0]['lr']
                avg_lm_loss = total_lm_loss.item() / args.log_interval
                elapsed_time = timers('interval time').elapsed()
                report_iteration_metrics(None, optimizer, learning_rate, avg_lm_loss,
                                         elapsed_time * 1000.0 / args.log_interval, args.iteration, args.train_iters,
                                         args)
                total_lm_loss = 0.0

            # Checkpointing
            if args.save and args.save_interval and args.iteration % args.save_interval == 0:
                save_checkpoint(args.iteration, model, optimizer, lr_scheduler, args)

            # Evaluation
            if args.eval_interval and args.iteration % args.eval_interval == 0:
                prefix = 'iteration {}'.format(args.iteration)
                evaluate_and_print_results(prefix, valid_dataloader, model, args, timers, False,
                                           forward_step_func=cross_entropy_forward_step)

        # Checkpointing at the end of each epoch.
        if args.save:
            save_checkpoint(args.iteration, model, optimizer, lr_scheduler, args)

        # Callback at the end of each epoch.
        if end_of_epoch_callback is not None:
            end_of_epoch_callback(model, epoch)


def finetune(args, train_valid_datasets_provider, model_type,
             forward_step=cross_entropy_forward_step,
             end_of_epoch_callback_provider=None):
    """Main finetune function used across all tasks."""
    timers = Timers()
    tokenizer = prepare_tokenizer(args)
    # Train and validation data loaders.
    timers('train/valid/test dataset/dataloder').start()
    train_dataloader, valid_dataloader = None, None
    if args.epochs > 0:
        train_dataset, valid_dataset = train_valid_datasets_provider(args, tokenizer)
        train_dataloader, valid_dataloader = _build_train_valid_dataloaders(
            train_dataset, valid_dataset, args)
    timers('train/valid/test dataset/dataloder').stop()

    # Build calback function.
    timers('callback function').start()
    end_of_epoch_callback = None
    if end_of_epoch_callback_provider is not None:
        end_of_epoch_callback = end_of_epoch_callback_provider(args, tokenizer)
    timers('callback function').stop()

    # Build model, optimizer and learning rate scheduler.
    timers('model and optimizer').start()
    model, optimizer, lr_scheduler = setup_model_and_optimizer(args, model_type)
    timers('model and optimizer').stop()

    # If pretrained checkpoint is provided and we have not trained for
    # any iteration (i.e., iteration is zero), then load the pretrained
    # checkpoint.
    timers('pretrained checkpoint').start()
    if args.load is not None:
        module = model
        if isinstance(module, (LocalDDP, TorchDDP)):
            module = module.module
        if isinstance(module, FP16_Module):
            module = module.module
        load_checkpoint(module.model, optimizer, lr_scheduler, args)
        # This is critical when only model is loaded. We should make sure
        # master parameters are also updated.
        if args.fp16:
            optimizer._model_params_to_master_params()
    timers('pretrained checkpoint').stop()

    # Print setup timing.
    print_rank_0('done with setups ...')
    timers.log(['train/valid/test dataset/dataloder', 'callback function',
                'model and optimizer', 'pretrained checkpoint'])
    print_rank_0('training ...')

    # Finetune the model.
    if args.epochs > 0:
        _train(model, optimizer, lr_scheduler, forward_step,
               train_dataloader, valid_dataloader, end_of_epoch_callback, args, timers)
    # Or just evaluate.
    else:
        if end_of_epoch_callback is not None:
            print_rank_0('evaluation only mode, setting epoch to -1')
            end_of_epoch_callback(model, epoch=-1, output_predictions=True)

    print_rank_0('done :-)')


if __name__ == '__main__':
    # Disable CuDNN.
    torch.backends.cudnn.enabled = False

    # Arguments.
    args = get_args()
    assert args.finetune

    # Pytorch distributed.
    initialize_distributed(args)

    # Random seeds for reproducability.
    set_random_seed(args.seed)

    if args.task == 'RACE':
        from tasks.race.finetune import main
    else:
        raise NotImplementedError('Task {} is not implemented.'.format(args.task))

    main(args)