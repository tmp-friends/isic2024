from pathlib import Path
import logging
import os
import gc

import random
import math
import copy
import time
import glob
from collections import defaultdict
import joblib
from tqdm import tqdm

import hydra

import numpy as np
import pandas as pd

from matplotlib import pyplot as plt
import cv2

# Pytorch Imports
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader, WeightedRandomSampler

# Sklearn Imports
from sklearn.model_selection import StratifiedKFold, StratifiedGroupKFold


from utils.utils import set_seed, save_processed_img
from utils.metrics import score_p_auc_with_torch
from conf.type import TrainConfig
from datasets.dataset import ISICDataset, PseudoISICDataset
from models.common import get_model


def load_data(cfg: TrainConfig):
    train_df = pd.read_csv(os.path.join(cfg.dir.data_dir, "train-metadata.csv"))

    pos_train_df = train_df[train_df["target"] == 1].reset_index(drop=True)
    neg_train_df = train_df[train_df["target"] == 0].reset_index(drop=True)

    # positive:negative=1:20になるようDown sampling
    # positiveが少ない不均衡データなので学習がうまくいくようにする意図
    train_df = pd.concat([pos_train_df, neg_train_df.iloc[: pos_train_df.shape[0] * 20, :]]).reset_index(drop=True)

    test_df = pd.read_csv(os.path.join(cfg.dir.data_dir, "test-metadata.csv"))
    test_df["target"] = 0  # dummy label

    return train_df, test_df


def get_sampler(df: pd.DataFrame):
    """ref: https://www.kaggle.com/code/syzygyfy/addressing-the-class-imbalance-in-pytorch"""
    u, c = np.unique(df["target"], return_counts=True)
    class_weights = [1.0 / v for v in c]
    sample_weights = [class_weights[v] for v in df["target"]]

    sampler = WeightedRandomSampler(sample_weights, len(sample_weights))

    return sampler


def fetch_scheduler(cfg: TrainConfig, optimizer: optim) -> lr_scheduler:
    if cfg.scheduler == "CosineAnnealingLR":
        scheduler = lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=cfg.T_max,
            eta_min=cfg.min_lr,
        )
    elif cfg.scheduler == "CosineAnnealingWarmRestarts":
        scheduler = lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer,
            T_0=cfg.T_0,
            eta_min=cfg.min_lr,
        )
    elif cfg.scheduler == None:
        return None

    return scheduler


def criterion(outputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
    return nn.BCELoss()(outputs, targets)


def infer_pseudo_label(cfg: TrainConfig, test_df: pd.DataFrame, model):
    test_dataset = ISICDataset(
        cfg=cfg,
        df=test_df,
        file_path=os.path.join(cfg.dir.data_dir, "test-image.hdf5"),
        is_training=False,
    )
    test_loader = DataLoader(
        test_dataset,
        batch_size=cfg.valid_batch_size,
        num_workers=4,
        shuffle=False,
        pin_memory=True,
    )

    preds = []
    with torch.no_grad():
        bar = tqdm(enumerate(test_loader), total=len(test_loader))
        for step, data in bar:
            images = data["image"].to(device, dtype=torch.float)
            batch_size = images.size(0)
            outputs = model(images)

            preds.append(outputs.detach().cpu().numpy())

    preds = np.concatenate(preds).flatten()

    return preds


def train_one_epoch(
    cfg: TrainConfig,
    model: nn.Module,
    optimizer: optim,
    scheduler: lr_scheduler,
    dataloader: DataLoader,
    epoch: int,
    test_df=None,
) -> tuple[torch.Tensor, torch.Tensor]:
    model.train()

    dataset_size = 0
    running_loss = 0.0
    running_pauc = 0.0

    y_true = []
    y_preds = []

    bar = tqdm(enumerate(dataloader), total=len(dataloader))
    for step, data in bar:
        images = data["image"].to(device, dtype=torch.float)
        targets = data["target"].to(device, dtype=torch.float)

        batch_size = images.size(0)

        outputs = model(images).squeeze()

        loss = criterion(outputs, targets)
        loss /= cfg.n_accumulates

        loss.backward()

        if (step + 1) % cfg.n_accumulates == 0:
            optimizer.step()

            # zero the parameter gradients
            optimizer.zero_grad()

            if scheduler is not None:
                scheduler.step()

        running_loss += loss.item() * batch_size
        dataset_size += batch_size

        y_true.extend(targets.detach().cpu().numpy())
        y_preds.extend(outputs.detach().cpu().numpy())

        bar.set_postfix(Epoch=epoch, LR=optimizer.param_groups[0]["lr"])

    epoch_loss = running_loss / dataset_size
    epoch_pauc = score_p_auc_with_torch(y_true=y_true, y_preds=y_preds)

    return epoch_loss, epoch_pauc


@torch.inference_mode()
def valid_one_epoch(
    model: nn.Module,
    optimizer: optim,
    dataloader: DataLoader,
    epoch: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    model.eval()

    dataset_size = 0
    running_loss = 0.0

    y_true = []
    y_preds = []
    bar = tqdm(enumerate(dataloader), total=len(dataloader))
    for step, data in bar:
        images = data["image"].to(device, dtype=torch.float)
        targets = data["target"].to(device, dtype=torch.float)

        batch_size = images.size(0)

        outputs = model(images).squeeze()
        loss = criterion(outputs, targets)

        running_loss += loss.item() * batch_size
        dataset_size += batch_size

        y_true.extend(targets.detach().cpu().numpy())
        y_preds.extend(outputs.detach().cpu().numpy())

        bar.set_postfix(Epoch=epoch, LR=optimizer.param_groups[0]["lr"])

    epoch_loss = running_loss / dataset_size
    epoch_pauc = score_p_auc_with_torch(y_true=y_true, y_preds=y_preds)

    return epoch_loss, epoch_pauc


def run_training(
    cfg: TrainConfig,
    model: nn.Module,
    optimizer: optim,
    scheduler: lr_scheduler,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    valid_loader: DataLoader,
) -> nn.Module | dict:
    if torch.cuda.is_available():
        LOGGER.info("[INFO] Using GPU: {}\n".format(torch.cuda.get_device_name()))

    start = time.time()
    best_model_wts = copy.deepcopy(model.state_dict())
    best_epoch_pauc = -np.inf
    history = defaultdict(list)

    for epoch in range(1, cfg.n_epochs + 1):
        if epoch <= 1:
            train_dataset = ISICDataset(
                cfg=cfg,
                df=train_df,
                file_path=os.path.join(cfg.dir.data_dir, "train-image.hdf5"),
                is_training=True,
            )
        else:  # PseudoLabeling
            test_df["target"] = infer_pseudo_label(cfg, test_df, model)

            train_dataset = PseudoISICDataset(
                cfg=cfg,
                df=train_df,
                file_path=os.path.join(cfg.dir.data_dir, "train-image.hdf5"),
                pseudo_df=test_df,
                pseudo_file_path=os.path.join(cfg.dir.data_dir, "test-image.hdf5"),
                pseudo_threshold=0.85,
                is_training=True,
            )

        train_loader = DataLoader(
            train_dataset,
            batch_size=cfg.train_batch_size,
            num_workers=4,
            # sampler=sampler,
            shuffle=True,
            pin_memory=True,
            drop_last=True,
        )

        train_epoch_loss, train_epoch_pauc = train_one_epoch(
            cfg,
            model,
            optimizer,
            scheduler,
            dataloader=train_loader,
            epoch=epoch,
        )
        valid_epoch_loss, valid_epoch_pauc = valid_one_epoch(
            model,
            optimizer,
            valid_loader,
            epoch=epoch,
        )
        LOGGER.info(
            f"Epoch {epoch}: Train pAUC: {train_epoch_pauc:.6f} - Valid pAUC: {valid_epoch_pauc:.6f} | Train Loss: {train_epoch_loss:.6f} - Valid Loss: {valid_epoch_loss:.6f}\n"
        )

        history["Train Loss"].append(train_epoch_loss)
        history["Valid Loss"].append(valid_epoch_loss)
        history["Train pAUC"].append(train_epoch_pauc)
        history["Valid pAUC"].append(valid_epoch_pauc)
        history["lr"].append(scheduler.get_last_lr()[0])

        # deep copy the model
        if best_epoch_pauc <= valid_epoch_pauc:
            LOGGER.info(f"Val pAUC Improved ({best_epoch_pauc} ---> {valid_epoch_pauc})")

            best_epoch_pauc = valid_epoch_pauc
            best_model_wts = copy.deepcopy(model.state_dict())

            PATH = "pAUC{:.4f}_Loss{:.4f}_epoch{:.0f}.bin".format(valid_epoch_pauc, valid_epoch_loss, epoch)
            torch.save(model.state_dict(), PATH)

            # Save a model file from the current directory
            LOGGER.info("Model saved")

    end = time.time()
    time_elapsed = end - start
    LOGGER.info(
        "Training complete in {:.0f}h {:.0f}m {:.0f}s".format(
            time_elapsed // 3600, (time_elapsed % 3600) // 60, (time_elapsed % 3600) % 60
        )
    )
    LOGGER.info("Best pAUC: {:.4f}".format(best_epoch_pauc))

    # load best model weights
    model.load_state_dict(best_model_wts)

    return model, history


@hydra.main(config_path="conf", config_name="train", version_base="1.2")
def main(cfg: TrainConfig):
    # Read meta
    df, test_df = load_data(cfg)
    LOGGER.info(df)

    cfg.T_max = df.shape[0] * (cfg.n_folds - 1) * cfg.n_epochs // cfg.train_batch_size // cfg.n_folds

    # Create fold
    sgkf = StratifiedGroupKFold(n_splits=cfg.n_folds)
    for fold, (_, val_) in enumerate(sgkf.split(df, df["target"], df["patient_id"])):
        df.loc[val_, "kfold"] = int(fold)

    # Create dataloader
    # sampler = get_sampler(df=df)
    train_df = df[df.kfold != cfg.fold].reset_index(drop=True)
    valid_df = df[df.kfold == cfg.fold].reset_index(drop=True)

    # PseudoLabelingのため、valid_loaderのみ生成しておく
    valid_dataset = ISICDataset(
        cfg=cfg,
        df=valid_df,
        file_path=os.path.join(cfg.dir.data_dir, "train-image.hdf5"),
        is_training=False,
    )
    valid_loader = DataLoader(
        valid_dataset,
        batch_size=cfg.valid_batch_size,
        num_workers=4,
        # sampler=sampler,
        shuffle=False,
        pin_memory=True,
    )

    # Def model
    model = get_model(cfg=cfg.model, is_pretrained=True)
    model.to(device)
    LOGGER.info(model)

    optimizer = optim.Adam(
        model.parameters(),
        lr=cfg.lr,
        weight_decay=cfg.weight_decay,
    )
    scheduler = fetch_scheduler(cfg=cfg, optimizer=optimizer)

    # Train
    model, history = run_training(
        cfg=cfg,
        model=model,
        optimizer=optimizer,
        scheduler=scheduler,
        train_df=train_df,
        test_df=test_df,
        valid_loader=valid_loader,
    )

    # Monitor
    history = pd.DataFrame.from_dict(history)
    history.to_csv("history.csv", index=False)

    plt.plot(range(history.shape[0]), history["Train Loss"].values, label="Train Loss")
    plt.plot(range(history.shape[0]), history["Valid Loss"].values, label="Valid Loss")
    plt.xlabel("epochs")
    plt.ylabel("Loss")
    plt.grid()
    plt.legend()
    plt.savefig("plt-loss.png")
    plt.clf()

    plt.plot(range(history.shape[0]), history["Train pAUC"].values, label="Train pAUC")
    plt.plot(range(history.shape[0]), history["Valid pAUC"].values, label="Valid pAUC")
    plt.xlabel("epochs")
    plt.ylabel("pAUC")
    plt.grid()
    plt.legend()
    plt.savefig("plt-p-auc.png")
    plt.clf()

    plt.plot(range(history.shape[0]), history["lr"].values, label="lr")
    plt.xlabel("epochs")
    plt.ylabel("lr")
    plt.grid()
    plt.legend()
    plt.savefig("plt-lr.png")
    plt.clf()


if __name__ == "__main__":
    # Logger
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s:%(name)s - %(message)s")
    LOGGER = logging.getLogger(Path(__file__).name)

    # For descriptive error messages
    os.environ["CUDA_LAUNCH_BLOCKING"] = "1"

    # Set GPU device
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    set_seed()

    main()
