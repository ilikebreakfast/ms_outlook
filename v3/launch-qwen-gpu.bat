@echo off
title Qwen GPU Core Initializer
color 0A

echo ===================================================
echo [1/4] Auditing Ollama Model Manifest...
echo ===================================================
:: Check if the foundational vision model is downloaded
ollama list | findstr "qwen2.5-vl" >nul
if %errorlevel% neq 0 (
    echo [MISSING] Qwen2.5-VL base engine not found.
    echo Pulling manifest layers from server library...
    echo.
    ollama pull qwen2.5-vl
) else (
    echo [SUCCESS] Found Qwen2.5-VL base library.
)

echo.
echo ===================================================
echo [2/4] Generating Local Modelfile...
echo ===================================================
(
echo FROM qwen2.5-vl
echo PARAMETER num_gpu 99
) > Modelfile
echo [SUCCESS] Modelfile parameters written to disk.

echo.
echo ===================================================
echo [3/4] Registering Profile (qwen-gpu)...
echo ===================================================
ollama create qwen-gpu -f Modelfile

echo.
echo ===================================================
echo [4/4] Purging Deployment Artifacts...
echo ===================================================
del Modelfile
echo [SUCCESS] Temporary configuration files purged.
echo.
echo Launching local execution interface...
timeout /t 2 >nul
cls

ollama run qwen-gpu