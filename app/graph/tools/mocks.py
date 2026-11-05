import json
from typing import Optional

def check_stock(args: dict, **kwargs) -> str:
    """
    Check if an item is in stock at the Hardware Store.
    """
    item_name = args.get("item_name", "")
    if not item_name: return json.dumps({"error": "No item_name provided"})
    
    # Mock inventory
    inventory = {
        "cement": "500 bags available. Price: 850 HTG.",
        "rebar": "1000 units (1/2 inch) available.",
        "paint": "White and Blue in stock. Red is out of stock.",
    }
    
    key = item_name.lower()
    for k, v in inventory.items():
        if k in key:
            return json.dumps({"item": item_name, "status": v})
            
    return json.dumps({"item": item_name, "status": f"Status for '{item_name}': In stock (General inventory)."})


def order_delivery(args: dict, **kwargs) -> str:
    """
    Schedule a delivery for hardware supplies.
    """
    item_name = args.get("item_name", "")
    address = args.get("address", "")
    phone = args.get("phone", "")
    
    if not item_name or not address or not phone:
        return json.dumps({"error": "Missing item_name, address, or phone"})

    return json.dumps({
        "status": "success",
        "message": f"Delivery confirmed for '{item_name}' to {address}. Driver will call {phone}."
    })


def schedule_viewing(args: dict, **kwargs) -> str:
    """
    Schedule a request to view a real estate property.
    """
    property_id = args.get("property_id", "")
    date = args.get("date", "")
    
    if not property_id or not date:
        return json.dumps({"error": "Missing property_id or date"})
        
    return json.dumps({
        "status": "success",
        "message": f"Viewing request received for {property_id} on {date}. An agent will confirm shortly."
    })
