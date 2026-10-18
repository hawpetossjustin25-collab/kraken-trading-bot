@echo off
title Kraken Trading Bot (supervised)
cd /d "%~dp0"
pip show requests flask >nul 2>&1 || pip install -r requirements.txt
python run_bot.py
pause
