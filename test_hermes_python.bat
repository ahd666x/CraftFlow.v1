@echo off
cd /d C:\Users\Hossein Nezhad\AppData\Local\hermes\bin
uvx.exe --from hermes-agent[mcp] python -c "import sys; print(sys.executable); print(sys.version)"