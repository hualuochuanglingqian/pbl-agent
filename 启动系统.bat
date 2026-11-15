@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在启动 PBL 医学教案工作台 ...
streamlit run app.py
