import subprocess
import sys

def run_bot():
    return subprocess.Popen([sys.executable, "discordbot.py"])

def run_api():
    return subprocess.Popen([sys.executable, "apiserver.py"])

if __name__ == "__main__":
    print("Lancement du bot Discord et de l'API FastAPI...")
    api_proc = run_api()
    bot_proc = run_bot()
    try:
        bot_proc.wait()
        api_proc.wait()
    except KeyboardInterrupt:
        print("Arrêt demandé (CTRL+C), fermeture...")
        bot_proc.terminate()
        api_proc.terminate()
