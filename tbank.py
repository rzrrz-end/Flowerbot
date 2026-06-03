import hashlib
import httpx
import logging
import json
from config import TBANK_TERMINAL_KEY, TBANK_SECRET_KEY

TBANK_API_URL = "https://securepay.tinkoff.ru/v2/"

def generate_token(params: dict) -> str:
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    token_string = ''.join(str(v) for _, v in sorted_params)
    return hashlib.sha256(token_string.encode('utf-8')).hexdigest()

async def create_payment(order_id: str, amount: float, description: str,
                         cart: list = None, customer_email: str = "",
                         customer_phone: str = "") -> str:
    amount_kopecks = int(amount * 100)

    payload = {
        "Amount": amount_kopecks,
        "Description": description,
        "OrderId": order_id,
        "Password": TBANK_SECRET_KEY,
        "SuccessURL": "https://t.me/@flowerbotsssbot",
        "TerminalKey": TBANK_TERMINAL_KEY,
    }

    payload["Token"] = generate_token(payload)
    del payload["Password"]

    # Чек для 54-ФЗ
    items = []
    if cart:
        for item in cart:
            items.append({
                "Name": item.get("name", "Товар")[:128],
                "Price": int(item.get("price", 0)) * 100,
                "Quantity": item.get("quantity", 1),
                "Amount": int(item.get("price", 0)) * item.get("quantity", 1) * 100,
                "Tax": "none"
            })
    else:
        items.append({
            "Name": description[:128],
            "Price": amount_kopecks,
            "Quantity": 1,
            "Amount": amount_kopecks,
            "Tax": "none"
        })

    payload["Receipt"] = {
        "Email": customer_email,
        "Phone": customer_phone,
        "Items": items,
        "Taxation": "osn"
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(TBANK_API_URL + "Init", json=payload)
        data = resp.json()
        logging.info(f"Ответ Т-Банка: {data}")
        if data.get("Success"):
            return data["PaymentURL"]
        else:
            raise Exception(f"Ошибка: {data.get('Message')}")
