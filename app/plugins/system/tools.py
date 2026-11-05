from app.plugins import register_tool

@register_tool(
    name="macro_sys_campaign",
    description="List active RapidPro campaigns.",
    trigger="sysadmin campaign list",
    near_miss=["[*] campaign [*]"],
    admin_only=True,
)
def sys_campaign(args: dict, **kw) -> str:
    """List active campaigns from RapidPro."""
    import httpx
    import os
    
    api_url = os.getenv("RAPIDPRO_API_URL", "http://localhost:8080/api/v2")
    api_token = os.getenv("RAPIDPRO_API_TOKEN", "")
    
    if not api_token:
        return "❌ RAPIDPRO_API_TOKEN not configured."
    
    try:
        resp = httpx.get(
            f"{api_url}/campaigns.json",
            headers={"Authorization": f"Token {api_token}"},
            timeout=5.0,
        )
        resp.raise_for_status()
        campaigns = resp.json().get("results", [])
        if not campaigns:
            return "📭 No active campaigns."
        
        msg = "📋 *Campaigns*:\n"
        for c in campaigns:
            msg += f"• `{c['name']}` (group: {c.get('group', {}).get('name', '?')})\n"
        return msg
    except Exception as e:
        return f"❌ Campaign query failed: {e}"


@register_tool(
    name="macro_sys_broadcast",
    description="Broadcast a message to a RapidPro group (confirmation required).",
    trigger="sysadmin broadcast",
    admin_only=True,
)
def sys_broadcast(args: dict, **kw) -> str:
    """Broadcast with confirmation guard — requires 'confirm' as second arg."""
    action_args = args.get("_args", [])
    if not action_args or action_args[0] != "confirm":
        return (
            "⚠️ *Broadcast is a dangerous operation.*\n"
            "To proceed, type: `sysadmin broadcast confirm <group_name> <message>`\n\n"
            "This will be migrated to an L1 RapidPro flow in a future release."
        )
    # Future: implement confirmed broadcast via RapidPro API
    return "🚧 Broadcast execution pending L1 flow implementation."
