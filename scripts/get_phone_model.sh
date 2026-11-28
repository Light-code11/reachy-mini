#!/bin/bash
# Download the YOLO11n object detector (Ultralytics, ~11 MB) that lets Reachy spot a held-up phone in check-in mode.
set -eu
cd "$(dirname "$0")/.."
mkdir -p models
URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx
[ -s models/yolo11n.onnx ] && { echo "models/yolo11n.onnx already there"; exit 0; }
curl -fL --retry 2 -o models/yolo11n.onnx.part "$URL"
mv models/yolo11n.onnx.part models/yolo11n.onnx
echo "Saved models/yolo11n.onnx"
