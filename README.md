# Phoenix_Bolotni 🐦

Bird species identification system based on audio classification. This project uses a deep learning approach to identify birds by their songs/calls, utilizing a pre-trained EfficientNet-B0 model acting on Log-Mel Spectrograms.

## 🚀 How it Works

1. **Audio Processing**: Audio files are resampled to 32,000 Hz and cropped/padded to a 5-second clip.
2. **Feature Extraction**: The audio is converted into a Log-Mel Spectrogram (128 Mel bins, 1024 FFT size, 320 hop length).
3. **Normalization**: The spectrogram is normalized to have zero mean and unit variance.
4. **Classification**: The normalized spectrogram is treated as a 3-channel image and passed through an **EfficientNet-B0** model (pre-trained on ImageNet), which classifies the bird species.

---

## 🛠 How to Fine-Tune on New Birds

If you want to add new bird species or improve the model's accuracy, follow these steps:

### 1. Prepare the Dataset
Organize your audio files in the following directory structure:
```text
dataset/
├── species_a/
│   ├── clip1.wav
│   ├── clip2.wav
│   └── ...
├── species_b/
│   ├── clip1.wav
│   └── ...
└── new_bird_species/
    ├── clip1.wav
    └── ...
```
- **Format**: Files must be in `.wav` format.
- **Content**: Ensure you have as many samples as possible for each species (aim for 100-200 clips per class for better results).

### 2. Setup the Environment
You will need Python 3.10+ and a GPU (NVIDIA) for efficient training.
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install tqdm scikit-learn soundfile matplotlib
```

### 3. Training the Model
Use the provided Jupyter Notebooks (found in the `train/` directory of the original source):
1. **`prepairing_dataset_sipuha.ipynb`**: Run this first to verify your dataset, check for missing classes, and generate metadata (`classes.json`, `label2idx.json`).
2. **`train_sipuha.ipynb`**: 
   - Set `DATA_DIR` to your dataset path.
   - Ensure `USE_IMAGENET_WEIGHTS = True` for faster convergence.
   - Run the training loop. The script uses **SpecAugment** and **WeightedRandomSampling** to handle imbalances.
   - The best model based on Macro-F1 score will be saved as `best.pth`.

### 4. Exporting for Android
To use the trained model in the Android app:
1. **Convert to ONNX**: Use `torch.onnx.export` to convert the `.pth` model to the ONNX format.
   ```python
   import torch
   # Load model architecture and state_dict from best.pth
   # Create a dummy input [1, 3, 128, 501]
   torch.onnx.export(model, dummy_input, "bird_model.onnx", opset_version=11)
   ```
2. **Integrate**: Replace the `.onnx` file in the Android project's assets folder with your new model.
3. **Update Labels**: Ensure the `classes.json` labels in the app match the order of the labels used during training.

---

## 📊 Technical Specifications

| Parameter | Value |
| :--- | :--- |
| Sample Rate | 32,000 Hz |
| Clip Duration | 5.0 seconds |
| FFT Size | 1024 |
| Hop Length | 320 |
| Mel Bins | 128 |
| Base Model | EfficientNet-B0 |
| Target Format | ONNX |
