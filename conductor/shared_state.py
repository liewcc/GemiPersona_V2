# shared_state.py
# Module-level singletons

lama_status: dict = {"ready": False, "error": None, "skipped": False}

import torch
from lama_refiner import LaMaRefiner
from inverse_alpha_compositing import InverseAlphaCompositing

_shared_remover = None
_shared_refiner = None

def get_shared_remover():
    global _shared_remover
    if _shared_remover is None:
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _shared_remover = InverseAlphaCompositing(os.path.join(base_dir, "assets", "sys_img", "bg_48.png"), os.path.join(base_dir, "assets", "sys_img", "bg_96.png"))
    return _shared_remover

def get_shared_refiner(use_gpu=True):
    global _shared_refiner
    if _shared_refiner is None:
        refiner = LaMaRefiner()
        target_device = "cuda" if use_gpu and torch.cuda.is_available() else "cpu"
        refiner.load_model(force_device=target_device)
        _shared_refiner = refiner
    return _shared_refiner

def clear_shared_refiner():
    global _shared_refiner
    _shared_refiner = None
