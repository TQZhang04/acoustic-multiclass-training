# pylint: disable=R0902
# Disables number of instance attributes
# Could be simplifed in future and more put into config
# but for MVP ignore this for now

""" Contains methods for loading the dataset and also creates dataloaders for training and validation

    BirdCLEFDataset is a generic loader with a given root directory.
    It loads the audio files and converts them to mel spectrograms.
    get_datasets returns the train and validation datasets as BirdCLEFDataset objects.

    If this module is run directly, it tests that the dataloader works and prints the shape of the first batch.

"""
import os
from typing import Dict, List, Tuple

import torch
import torch.nn.functional as F
from torch.utils.data import Dataset
import torchaudio
from torchaudio import transforms as audtr
from torchvision import transforms as vitr
import pandas as pd

# Math library imports
import numpy as np

from utils import set_seed, print_verbose
import utils
from config import get_config
import config
from tqdm import tqdm
from augmentations import Mixup, SyntheticNoise
        
cfg = config.cfg

tqdm.pandas()
device = 'cuda' if torch.cuda.is_available() else 'cpu'

#https://www.kaggle.com/code/debarshichanda/pytorch-w-b-birdclef-22-starter
class PyhaDF_Dataset(Dataset):
    """
        Dataset designed to work with pyha output
        Save unchunked data
    """

    # df, csv_file, train, and species decided outside of config, so those cannot be added in there
    # pylint: disable-next=too-many-instance-attributes
    # pylint: disable-next=too-many-arguments
    def __init__(self, df, csv_file="test.csv", train=True, species=None):
        self.samples = df[~(df[cfg.file_name_col].isnull())]
        self.csv_file = csv_file
        self.formatted_csv_file = "not yet formatted"
        self.target_sample_rate = cfg.sample_rate
        num_samples = self.target_sample_rate * cfg.max_time
        self.num_samples = num_samples
        self.train = train


        self.mel_spectogram = audtr.MelSpectrogram(sample_rate=self.target_sample_rate,
                                        n_mels=cfg.n_mels,
                                        n_fft=cfg.n_fft)
        self.mel_spectogram.to(device) #was cuda (?)
        self.freq_mask = audtr.FrequencyMasking(freq_mask_param=cfg.freq_mask_param)
        self.time_mask = audtr.TimeMasking(time_mask_param=cfg.time_mask_param)
        self.transforms = None
        self.mixup = None

        # List data directory and confirm it exists
        if not os.path.exists(cfg.data_path):
            raise FileNotFoundError("Data path does not exist")
        self.data_dir = set(os.listdir(cfg.data_path))
        
        #Log bad files
        self.bad_files = []

        #Preprocessing start
        if species is not None:
            self.classes, self.class_to_idx = species
        else:
            self.classes = self.samples[cfg.manual_id_col].unique()
            class_idx = np.arange(len(self.classes))
            self.class_to_idx = dict(zip(self.classes, class_idx))

        self.num_classes = len(self.classes)
        self.serialize_data()

    def verify_audio(self):
        """
        Checks to make sure files exist that are referenced in input df
        """
        missing_files = pd.Series(self.samples[cfg.file_name_col].unique()) \
            .progress_apply(lambda file: "good" if file in self.data_dir else file)
        missing_files = missing_files[missing_files != "good"].unique()
        print("ignoring", missing_files.shape[0], "missing files")
        self.samples = self.samples[
            ~self.samples[cfg.file_name_col].isin(missing_files)
        ]

    def process_audio_file(self, file_name):
        """
        Save waveform of audio file as a tensor and save that tensor to .pt
        """

        exts = "." + file_name.split(".")[-1]
        new_name = file_name.replace(exts, ".pt")
        if new_name in self.data_dir:
            #ASSUME WE HAVE ALREADY PREPROCESSED THIS CORRECTLY
            return pd.Series({
                "FILE NAME": file_name,
                "files": new_name
            }).T


        try:
            # old error: "load" is not a known member of module "torchaudio"
            # Load is a known member of torchaudio:
            # https://pytorch.org/audio/stable/tutorials/audio_io_tutorial.html#loading-audio-data
            audio, sample_rate = torchaudio.load(       #pyright: ignore [reportGeneralTypeIssues ]
                os.path.join(cfg.data_path, file_name)
            ) 

            if len(audio.shape) > 1:
                audio = self.to_mono(audio)

            # Resample
            if sample_rate != self.target_sample_rate:
                resample = audtr.Resample(sample_rate, self.target_sample_rate)
                #resample.cuda(device)
                audio = resample(audio)

            torch.save(audio, os.path.join(cfg.data_path,new_name))
            self.data_dir.add(new_name)
        # IO is messy, I want any file that could be problematic
        # removed from training so it isn't stopped after hours of time
        # Hence broad exception
        # pylint: disable-next=W0718
        except Exception as e:
            print_verbose(file_name, "is bad", e, verbose=cfg.verbose)
            return pd.Series({
                "FILE NAME": file_name,    
                "files": "bad"
            }).T


        return pd.Series({
                "FILE NAME": file_name,    
                "files": new_name
            }).T


    def serialize_data(self):
        """
        For each file, check to see if the file is already a presaved tensor
        If the files is not a presaved tensor and is an audio file, convert to tensor to make
        Future training faster
        """
        self.verify_audio()
        files = pd.DataFrame(self.samples[cfg.file_name_col].unique(),
            columns=["files"]
        )
        files = files["files"].progress_apply(self.process_audio_file)

        print(files.shape, flush=True)

        num_files = files.shape[0]
        if num_files == 0:
            raise FileNotFoundError("There were no valid filepaths found, check csv")

        files = files[files["files"] != "bad"]
        self.samples = self.samples.merge(files, how="left", 
                       left_on=cfg.file_name_col,
                       right_on="FILE NAME").dropna()
    
        print_verbose("Serialized form, fixed size:", self.samples.shape, verbose=cfg.verbose)

        if "files" in self.samples.columns:
            self.samples[cfg.file_name_col] = self.samples["files"].copy()
        if "files_y" in self.samples.columns:
            self.samples[cfg.file_name_col] = self.samples["files_y"].copy()
        
        self.samples["original_file_path"] = self.samples[cfg.file_name_col]

        self.formatted_csv_file = ".".join(self.csv_file.split(".")[:-1]) + "formatted.csv"
        self.samples.to_csv(self.formatted_csv_file)


    def __len__(self):
        return self.samples.shape[0]

    def to_image(self, audio):
        """
        Convert audio clip to 3-channel spectrogram image
        """
        # Mel spectrogram
        mel = self.mel_spectogram(audio)
        # Convert to Image
        image = torch.stack([mel, mel, mel])
        # Normalize Image
        max_val = torch.abs(image).max() + 0.000001
        image = image / max_val
        return image

    def __getitem__(self, index): #-> Any:
        """ Takes an index and returns tuple of spectrogram image with corresponding label
        """
        #TODO: don't initialize these every time
        audio_augmentations = vitr.RandomApply(torch.nn.Sequential(
                SyntheticNoise("white", 0.05)), p=1)
        image_augmentations = vitr.RandomApply(torch.nn.Sequential(
                audtr.FrequencyMasking(cfg.freq_mask_param),
                audtr.TimeMasking(cfg.time_mask_param)), p=0.4)


        audio, target = utils.get_annotation(
                df = self.samples,
                index = index,
                class_to_idx = self.class_to_idx,
                sample_rate = self.target_sample_rate,
                target_num_samples = self.num_samples,
                device = device)

        
        mixup = Mixup(
                df = self.samples,
                class_to_idx = self.class_to_idx,
                sample_rate = self.target_sample_rate,
                target_num_samples = self.num_samples,
                alpha_range = (0.1, 0.4),
                p = 0.4)
        
        if self.train:
            audio, target = mixup(audio, target)
            audio = audio_augmentations(audio)
        image = self.to_image(audio)
        if self.train:
            image = image_augmentations(image)

        if image.isnan().any():
            print("ERROR IN ANNOTATION #", index)
            self.bad_files.append(index)
            #try again with a diff annotation to avoid training breaking
            image, target = self[self.samples.sample(1).index[0]]

        return image, target

    def set_transforms(self, transforms):
        """ Sets the transforms for the dataset
        """
        self.transforms = transforms
    def set_mixup(self, mixup):
        """ Sets the mixup object for the dataset
        """
        self.mixup = mixup

    def pad_audio(self, audio: torch.Tensor) -> torch.Tensor:
        """Fills the last dimension of the input audio with zeroes until it is num_samples long
        """
        pad_length = self.num_samples - audio.shape[0]
        last_dim_padding = (0, pad_length)
        audio = F.pad(audio, last_dim_padding)
        return audio

    def crop_audio(self, audio: torch.Tensor) -> torch.Tensor:
        """Cuts audio to num_samples long
        """
        return audio[:self.num_samples]

    def to_mono(self, audio: torch.Tensor) -> torch.Tensor:
        """ Converts audio to mono by averaging the channels
        """
        return torch.mean(audio, dim=0)

    def get_classes(self) -> Tuple[List[str], Dict[str, int]]:
        """ Returns tuple of class list and class to index dictionary
        """
        return self.classes, self.class_to_idx

    def get_num_classes(self) -> int:
        """ Returns number of classes
        """
        return self.num_classes


def get_datasets():
    """ Returns train and validation datasets, does random sampling for train/valid split, adds transforms to dataset
    """
    train_p = cfg.train_test_split
    path = cfg.dataframe_csv
    # Load the dataset
    data = pd.read_csv(path, usecols = [
        cfg.file_name_col,
        cfg.manual_id_col,
        cfg.offset_col,
        cfg.duration_col
    ], dtype={
        cfg.file_name_col: str,
        cfg.manual_id_col: str,
        cfg.offset_col: float,
        cfg.duration_col: float})
    
    #for each species, get a random sample of files for train/valid split
    train_files = data.groupby(cfg.manual_id_col, as_index=False).apply(
        lambda x: pd.Series(x[cfg.file_name_col].unique()).sample(frac=train_p)
    )
    train = data[data[cfg.file_name_col].isin(train_files)]

    valid = data[~data.index.isin(train.index)]
    train_ds = PyhaDF_Dataset(train, csv_file="train.csv")
    species = train_ds.get_classes()

    valid_ds = PyhaDF_Dataset(valid, csv_file="valid.csv",train=False, species=species)
    return train_ds, valid_ds


def main():
    """
    testing function.
    """
    torch.multiprocessing.set_start_method('spawn')
    set_seed(cfg.seed)
    get_datasets()

if __name__ == '__main__':
    main()
