from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps


class ImagePreprocessor:
    def __init__(self, max_deskew_degrees: float = 15.0) -> None:
        self._max_deskew_degrees = max_deskew_degrees

    def prepare(self, image: Image.Image) -> Image.Image:
        oriented = ImageOps.exif_transpose(image).convert("RGB")
        grayscale = cv2.cvtColor(np.asarray(oriented), cv2.COLOR_RGB2GRAY)
        contrasted = np.asarray(ImageOps.autocontrast(Image.fromarray(grayscale)))
        _, binary = cv2.threshold(
            contrasted,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        deskewed = self._deskew(binary)
        return Image.fromarray(deskewed).convert("RGB")

    def _deskew(self, image: np.ndarray) -> np.ndarray:
        coordinates = np.column_stack(np.where(image < 255)[::-1]).astype(np.float32)
        if len(coordinates) < 20:
            return image

        angle = float(cv2.minAreaRect(coordinates)[-1])
        if angle > 45:
            angle -= 90
        if abs(angle) < 0.1 or abs(angle) > self._max_deskew_degrees:
            return image

        height, width = image.shape
        center = (width / 2, height / 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        return cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
