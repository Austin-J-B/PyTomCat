@echo off
echo Starting TomCat VI installation...

:: Create and activate virtual environment
echo Creating virtual environment...
python -m venv .venv
call .venv\Scripts\activate.bat

:: Upgrade pip and install dependencies
echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: Install additional packages needed for model conversion
echo Installing model conversion dependencies...
pip install transformers safetensors

:: Convert and setup the DeBERTa model
echo Setting up DeBERTa ONNX model...
python scripts\convert_model.py

:: Clean up unnecessary files
echo Cleaning up extra files...
cd weights
del /f /q added_tokens.json special_tokens_map.json spm.model tokenizer_config.json 2>nul
cd ..

:: Run test to verify everything works
echo Testing model installation...
python scripts\test_model.py

echo.
echo Installation complete! You can now run TomCat using:
echo python -m tomcat.main
echo.
echo Don't forget to:
echo 1. Set up your .env file with the required configuration
echo 2. Ensure you have the YOLO model weights (NanoModel.pt and NanoClassifier.pt) in the weights folder
echo.
pause