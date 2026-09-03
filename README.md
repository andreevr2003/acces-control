# Access Control: ESP32 + Raspberry Pi 5

ESP32 ramane la usa si pastreaza:

- PN532 si cardurile autorizate in NVS;
- releul, butonul si camera;
- API HTTP local pentru Raspberry Pi.

Raspberry Pi ruleaza Telegram in Docker. Fotografiile sunt trimise direct in
Telegram; nu se foloseste o baza de date sau un volum pentru loguri.

## Configurare ESP32

Copiati `include/secrets.example.h` ca `include/secrets.h` si completati SSID,
parola Wi-Fi, IP-ul Raspberry Pi si cheia API.

IP-ul trebuie sa fie accesibil din reteaua Wi-Fi a ESP32, nu IP-ul Docker
`172.17.x.x`. Exemplu: `http://192.168.1.20:8080/event`.

## Deploy Portainer

Creati un Stack din acest repository Git, cu fisierul:

```text
docker-compose.yml
```

Setati variabilele:

```text
BOT_TOKEN=tokenul_nou_al_botului
ALLOWED_CHAT_ID=1407961040
ESP_URL=http://IP-ul-ESP32
ACCESS_KEY=aceeasi-cheie-ca-in-secrets.h
```

Portul publicat este `8080:8080`. Botul raspunde la `/start` si `/menu`.

Nu comitati niciodata `.env`, `include/secrets.h` sau tokenul Telegram.
