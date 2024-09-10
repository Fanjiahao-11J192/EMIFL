set -e
run_idx=$1
pretrained_idx=$2
gpu=$3
transformer_heads=$4
transformer_layers=$5

for i in `seq 1 1 10`;
do

cmd="python train_miss.py --dataset_mode=multimodal_miss --model=EMFIL
--log_dir=./logs --checkpoints_dir=./checkpoints --gpu_ids=$gpu --image_dir=./shared_image
--A_type=acoustic --input_dim_a=74 --norm_method=trn --embd_size_a=128 --conv_dim_a=40 --embd_method_a=maxpool
--V_type=visual --input_dim_v=47 --embd_size_v=128 --conv_dim_v=40 --embd_method_v=maxpool
--L_type=bert_large --input_dim_l=768 --embd_size_l=128 --conv_dim_l=40
--transformer_heads=$transformer_heads --transformer_layers=$transformer_layers
--AE_layers=256,128,64 --n_blocks=5 --num_thread=0 --corpus=MOSI --corpus_name=MOSI
--ce_weight=1.0 --mse_weight=8.0 --trans_weight=100
--output_dim=1 --cls_layers=128,64 --dropout_rate=0.5
--niter=30 --niter_decay=30 --verbose --print_freq=10
--batch_size=32 --lr=2e-4 --run_idx=$run_idx --weight_decay=1e-5
--name=EMFIL_MOSI --suffix=block_{n_blocks}_run_{gpu_ids}_{run_idx} --has_test
--pretrained_path='checkpoints/MOSI_pretrained_EMIFL_AVL_run$pretrained_idx'
--cvNo=$i"


echo "\n-------------------------------------------------------------------------------------"
echo "Execute command: $cmd"
echo "-------------------------------------------------------------------------------------\n"
echo $cmd | sh

done
