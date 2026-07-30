"""Coleta e download de conteúdo de redes sociais (Instagram) via ego-lite.

Portado/adaptado do sistema IGSorter do usuário (~/IGSorter). O motor de coleta
usa a sessão logada do ego-lite — bem mais robusto que o yt-dlp para Instagram —
e entrega metadados ricos (ER, views, likes, comentários, hashtags, duração).

Os módulos aqui são deliberadamente independentes do FastAPI: o whisper-app.py
os importa e expõe os endpoints /api/social/*.
"""
