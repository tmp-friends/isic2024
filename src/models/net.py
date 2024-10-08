import torch
import torch.nn as nn
import torch.nn.functional as F

import timm

from models.functions import SwishModule


class Net(nn.Module):
    def __init__(
        self,
        model_name,
        pretrained=False,
        checkpoint_path=None,
        n_meta_features=0,
        n_meta_dim=[256, 32],
        num_classes=1,
    ):
        super().__init__()
        self.model = timm.create_model(model_name, pretrained=pretrained, checkpoint_path=checkpoint_path)

        self.n_meta_features = n_meta_features
        in_features = self.model.get_classifier().in_features

        if n_meta_features > 0:
            self.meta = nn.Sequential(
                nn.Linear(n_meta_features, n_meta_dim[0]),
                nn.BatchNorm1d(n_meta_dim[0]),
                SwishModule(),
                nn.Dropout(p=0.3),
                nn.Linear(n_meta_dim[0], n_meta_dim[1]),
                nn.BatchNorm1d(n_meta_dim[1]),
                SwishModule(),
            )
            in_features += n_meta_dim[1]
        self.linear = nn.Linear(in_features, num_classes)
        self.dropouts = nn.ModuleList([nn.Dropout(0.5) for _ in range(5)])
        self.sigmoid = nn.Sigmoid()

        self.model.reset_classifier(0)  # Remove the original classifier

    def extract(self, x):
        return self.model(x)

    def forward(self, x, x_meta):
        """
        Args:
            x: images
            x_meta: metadata
        """
        # swin
        x = self.extract(x)

        # average poolingを適用して特徴をバッチサイズ x 特徴量数に変換
        # x = torch.mean(x, dim=[2, 3])  # 平均値をとることで2次元の特徴量にする

        if self.n_meta_features > 0:
            x_meta = self.meta(x_meta)
            x = torch.cat((x, x_meta), dim=1)
        for i, dropout in enumerate(self.dropouts):
            if i == 0:
                out = self.linear(dropout(x))
            else:
                out += self.linear(dropout(x))

        out /= len(self.dropouts)
        out = self.sigmoid(out)

        return out
