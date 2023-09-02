import torch
from src.model_adv import DiffPrvBackdoorRegistrar

if __name__ == '__main__':
    path_to_registrar = './weights/test_dpbkd_rgs_ex0.pth'
    registrar = DiffPrvBackdoorRegistrar()
    registrar.load_information(torch.load(path_to_registrar))
    delta_bu_select = registrar.get_change_by_activation(activation_count=0)