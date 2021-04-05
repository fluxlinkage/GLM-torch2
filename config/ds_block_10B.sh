#! /bin/bash

script_path=$(realpath $BASH_SOURCE)
script_dir=$(dirname $script_path)

config_json="$script_dir/config_gpt_10B.json"
gpt_options=" \
       --block-lm \
       --task-mask \
       --bert-prob 0.4 \
       --gap-sentence-prob 0.3 \
       --avg-block-length 3 \
       --gpt-min-ratio 0.25 \
       --block-mask-prob 0.1 \
       --experiment-name blocklm-10b \
       --model-parallel-size ${MP_SIZE} \
       --num-layers 48 \
       --hidden-size 4096 \
       --num-attention-heads 64 \
       --seq-length 512 \
       --max-position-embeddings 1024 \
       --save /dataset/fd5061f6/english_data/checkpoints \
       --load /dataset/fd5061f6/english_data/checkpoints/blocklm-10b03-30-16-42 \
       --log-interval 50 \
       --eval-interval 1000 \
       --save-interval 2000 \
       --train-iters 250000 \
       --resume-dataloader \
       --train-data pile cc-news \
       --no-lazy-loader \
       --loader-scatter 32 \
       --tokenizer-type GPT2BPETokenizer \
       --split 949,50,1 \
       --distributed-backend nccl \
       --lr-decay-style cosine \
       --lr-decay-ratio 0.1 \
       --lr-decay-iters 175000 \
       --warmup 0.04 \
       --checkpoint-activations \
       --deepspeed-activation-checkpointing \
       --fp16 \
"
gpt_options="${gpt_options}
               --deepspeed \
               --deepspeed_config ${config_json} \
"