# coding: utf-8
__author__ = 'Roman Solovyev (ZFTurbo): https://github.com/ZFTurbo/'

import argparse
import time
import librosa
from tqdm import tqdm
import sys
import os
import glob
import torch
import numpy as np
import soundfile as sf
import torch.nn as nn

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from utils import demix_track, demix_track_demucs, get_model_from_config
import logging
import warnings

warnings.filterwarnings("ignore", category = UserWarning)
log_format = "%(asctime)s.%(msecs)03d [%(levelname)s] %(module)s - %(message)s"
date_format = "%H:%M:%S"
logging.basicConfig(level = logging.INFO, format = log_format, datefmt = date_format)
logger = logging.getLogger(__name__)

def run_folder(model, args, config, device, verbose = False):
    start_time = time.time()
    model.eval()
    all_mixtures_path = glob.glob(args.input_folder + '/*.*')
    logger.info('Total files found: {}'.format(len(all_mixtures_path)))

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)
    extra_store_dir = args.extra_store_dir
    if not os.path.isdir(extra_store_dir):
        extra_store_dir = args.store_dir

    if not verbose:
        all_mixtures_path = tqdm(all_mixtures_path, desc="Total progress")

    if 'vocals' not in instruments and args.extract_instrumental and len(config.training.instruments) != 2:
        logger.warning('Training instruments > 2, so secondary stem extraction is not possible')

    for path in all_mixtures_path:
        if not verbose:
            all_mixtures_path.set_postfix({'track': os.path.basename(path)})
        try:
            # mix, sr = sf.read(path)
            mix, sr = librosa.load(path, sr = 44100, mono = False)
        except Exception as e:
            logger.warning('Can read track: {}'.format(path))
            logger.warning('Error message: {}'.format(str(e)))
            continue

        # Convert mono to stereo if needed
        if len(mix.shape) == 1:
            mix = np.stack([mix, mix], axis=0)

        mix_orig = mix.copy()
        if 'normalize' in config.inference:
            if config.inference['normalize'] is True:
                mono = mix.mean(0)
                mean = mono.mean()
                std = mono.std()
                mix = (mix - mean) / std

        mixture = torch.tensor(mix, dtype=torch.float32)
        if args.model_type == 'htdemucs':
            res = demix_track_demucs(config, model, mixture, device, pbar=True)
        else:
            res = demix_track(config, model, mixture, device, pbar=True)
        for instr in instruments:
            estimates = res[instr].T
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = estimates * std + mean
            file_name, _ = os.path.splitext(os.path.basename(path))
            output_file = os.path.join(args.store_dir, f"{file_name}_{instr}.wav")
            sf.write(output_file, estimates, sr, subtype = 'FLOAT')

        if 'vocals' in instruments and args.extract_instrumental:
            file_name, _ = os.path.splitext(os.path.basename(path))
            instrum_file_name = os.path.join(extra_store_dir, f"{file_name}_instrumental.wav")
            estimates = res['vocals'].T
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = estimates * std + mean
            sf.write(instrum_file_name, mix_orig.T - estimates, sr, subtype = 'FLOAT')

        if 'vocals' not in instruments and args.extract_instrumental and config.training.target_instrument is not None:
            file_name, _ = os.path.splitext(os.path.basename(path))
            instrum_file_name = os.path.join(extra_store_dir, f"{file_name}_other.wav")
            estimates = res[config.training.target_instrument].T
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = estimates * std + mean
            sf.write(instrum_file_name, mix_orig.T - estimates, sr, subtype = 'FLOAT')

    time.sleep(0.5)
    logger.info("Elapsed time: {:.2f} sec".format(time.time() - start_time))
    logger.info('Results are saved to: {}'.format(args.store_dir))


def proc_folder(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_type", type=str, default='mdx23c', 
                        help="One of bandit, bandit_v2, bs_roformer, htdemucs, mdx23c, mel_band_roformer, scnet, scnet_unofficial, segm_models, swin_upernet, torchseg")
    parser.add_argument("--config_path", type = str, help = "path to config file")
    parser.add_argument("--start_check_point", type = str, default = '', help = "Initial checkpoint to valid weights")
    parser.add_argument("--input_folder", type = str, help = "folder with mixtures to process")
    parser.add_argument("--store_dir", default = "", type = str, help = "path to store results as wav file")
    parser.add_argument("--device", default = None, type = str, help="device to use for inference")
    parser.add_argument("--device_ids", nargs = '+', type = int, default = 0, help = 'list of gpu ids')
    parser.add_argument("--extract_instrumental", action = 'store_true',
                        help = "invert vocals to get instrumental if provided")
    parser.add_argument("--extra_store_dir", default = "", type = str, help = "path to store extracted instrumental. If not provided, store_dir will be used")
    parser.add_argument("--force_cpu", action = 'store_true', help = "Force the use of CPU even if CUDA is available")

    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    device = "cpu"
    if args.force_cpu:
        device = "cpu"
    elif torch.cuda.is_available():
        device = "cuda"
        device = f'cuda:{args.device_ids[0]}' if type(args.device_ids) == list else f'cuda:{args.device_ids}'
    elif torch.backends.mps.is_available():
        device = "mps"

    logger.info(f"Using device: {device}")
    torch.backends.cudnn.benchmark = True

    model, config = get_model_from_config(args.model_type, args.config_path)
    if args.start_check_point != '':
        logger.info('Start from checkpoint: {}'.format(args.start_check_point))
        if args.model_type == 'htdemucs':
            state_dict = torch.load(args.start_check_point, map_location = device, weights_only=False)
            # Fix for htdemucs pretrained models
            if 'state' in state_dict:
                state_dict = state_dict['state']
        else:
            state_dict = torch.load(args.start_check_point, map_location = device, weights_only=True)
        model.load_state_dict(state_dict)
    logger.info("Instruments: {}".format(config.training.instruments))
    
    # in case multiple CUDA GPUs are used and --device_ids arg is passed
    if type(args.device_ids) == list and len(args.device_ids) > 1 and not args.force_cpu:
        model = nn.DataParallel(model, device_ids = args.device_ids)
    model = model.to(device)

    run_folder(model, args, config, device, verbose = False)


if __name__ == "__main__":
    proc_folder(None)
