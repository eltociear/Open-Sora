import colossalai
import torch
import torch.distributed as dist
from opensora.models.stdit.stdit3 import STDiT3Config, STDiT3
from colossalai.testing import spawn, free_port
from opensora.acceleration.parallel_states import set_data_parallel_group, set_sequence_parallel_group
from colossalai.utils.common import set_seed


def get_sample_data():
    x = torch.rand([1, 4, 15, 20, 27], dtype=torch.bfloat16) # (B, C, T, H, W)
    timestep = torch.Tensor([924.]).to(torch.bfloat16)
    y = torch.rand(1, 1, 300, 4096, dtype=torch.bfloat16)
    mask = torch.ones([1, 300], dtype=torch.int32)
    x_mask = torch.ones([1, 15]).bool()
    fps = torch.Tensor([25.]).to(torch.bfloat16)
    height = torch.Tensor([166.]).to(torch.bfloat16)
    width = torch.Tensor([221.]).to(torch.bfloat16)
    return dict(x=x, timestep=timestep, y=y, mask=mask, x_mask=x_mask, fps=fps, height=height, width=width)

def get_stdit3_config(enable_sequence_parallelism = False):
    config = {
            "caption_channels": 4096,
            "class_dropout_prob": 0.0,
            "depth": 1,
            "drop_path": 0.0,
            "enable_flash_attn": True,
            "enable_layernorm_kernel": True,
            "enable_sequence_parallelism": enable_sequence_parallelism,
            "freeze_y_embedder": True,
            "hidden_size": 1152,
            "in_channels": 4,
            "input_size": [
                None,
                None,
                None
            ],
            "input_sq_size": 512,
            "mlp_ratio": 4.0,
            "model_max_length": 300,
            "model_type": "STDiT3",
            "num_heads": 16,
            "only_train_temporal": False,
            "patch_size": [
                1,
                2,
                2
            ],
            "pred_sigma": True,
            "qk_norm": True,
            "skip_y_embedder": False,
        }
    return STDiT3Config(**config)


def run_model(rank, world_size, port):
    colossalai.launch({}, rank=rank, world_size=world_size, port=port, host="localhost")
    
    # prepare data
    data = get_sample_data()
    data = {
        k: v.cuda()
        for k, v in data.items()
    }
    
    # test single-gpu outptu
    set_seed(1024)
    non_dist_model_cfg = get_stdit3_config(enable_sequence_parallelism=False)
    non_dist_model = STDiT3(non_dist_model_cfg).cuda().to(torch.bfloat16)
    non_dist_out = non_dist_model(**data)
    non_dist_out.mean().backward()
    
    # run seq parallelism
    set_sequence_parallel_group(dist.group.WORLD)
    set_seed(1024)
    dist_model_cfg = get_stdit3_config(enable_sequence_parallelism=True)
    dist_model = STDiT3(dist_model_cfg).cuda().to(torch.bfloat16)
    dist_out = dist_model(**data)
    dist_out.mean().backward()
    
    # run all reduce for gradients
    for param in dist_model.parameters():
        if param.grad is not None:
            dist.all_reduce(param.grad, group=dist.group.WORLD)
            param.grad /= world_size
    
    # ensure model weights are equal
    for (p1, p2) in zip(non_dist_model.parameters(), dist_model.parameters()):
        assert torch.equal(p1, p2)
    
    # check
    torch.testing.assert_close(non_dist_out, dist_out)
    for ((n1, p1), (n2, p2)) in zip(non_dist_model.named_parameters(), dist_model.named_parameters()):
        assert n1 == n2
        if p1.grad is not None and p2.grad is not None:
            if not torch.allclose(p1.grad, p2.grad,rtol=1e-2, atol=1e-4):
                if dist.get_rank() == 0:
                    print(f"gradient of {n1} is not equal, {p1.grad} vs {p2.grad}")
        else:
            assert p1.grad is None and p2.grad is None
    

def test_stdit3_sp():
    spawn(run_model, 2)
    

if __name__ == '__main__':
    test_stdit3_sp()
