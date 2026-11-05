from fastapi import APIRouter, Depends
from app.plugins import PLUGIN_REGISTRY
from app.api.adapters.tools import verify_internal_key

router = APIRouter(tags=["plugins"])

@router.get("/v1/system/plugins/manifest", dependencies=[Depends(verify_internal_key)])
async def get_plugin_manifest():
    """Returns plugin metadata for RiveBot dynamic configuration."""
    manifest = []
    for tool_name, meta in PLUGIN_REGISTRY.items():
        manifest.append({
            "name": meta["name"],
            "description": meta["description"],
            "admin_only": meta["admin_only"],
            "trigger": meta["trigger"],
            "near_miss": meta["near_miss"],
        })
    return {"plugins": manifest}
