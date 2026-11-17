@echo off
chcp 65001 >nul
cd /d "D:\医学知识库\personal_kb"
streamlit run app.py --server.port 8730
pause
