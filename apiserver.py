import os
import logging
from fastapi import FastAPI, Request, HTTPException, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Any
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("apiserver")

class Command(BaseModel):
    type: str
    args: Dict[str, Any]

app = FastAPI(title="Roblox Ban Bot API")

# Shared state, protected by locks
pending_commands: List[Dict[str, Any]] = []
ban_list_from_roblox: List[Any] = []
cmd_lock = asyncio.Lock()
ban_lock = asyncio.Lock()

@app.get("/")
async def root():
    return {
        "status": "Bot is running",
        "pending_commands": len(pending_commands),
        "total_bans": len(ban_list_from_roblox)
    }

@app.get("/get-commands")
async def get_commands():
    async with cmd_lock:
        cmds = list(pending_commands)
        pending_commands.clear()
    logger.info(f"[API] {len(cmds)} commande(s) envoyée(s)")
    return {"commands": cmds, "count": len(cmds)}

@app.post("/update-bans")
async def update_bans(ban_list: List[Any] = Body(...)):
    async with ban_lock:
        ban_list_from_roblox.clear()
        ban_list_from_roblox.extend(ban_list)
    logger.info(f"✅ Liste des bannissements mise à jour ({len(ban_list)} entrées).")
    return {"status": "success", "count": len(ban_list)}

@app.get("/get-bans")
async def get_bans():
    return {"banlist": ban_list_from_roblox, "count": len(ban_list_from_roblox)}

@app.post("/add-command")
async def add_command(command: Dict[str, Any]):
    # Accepte le format "plat" du bot ({action, username, ...})
    # comme l'ancien format ({type, args}) pour ne pas casser Roblox.
    async with cmd_lock:
        pending_commands.append(dict(command))
    logger.info(f"Commande ajoutée: {command}")
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
