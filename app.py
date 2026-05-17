import os
import json
import requests
import gspread
from datetime import datetime
from flask import Flask, request, jsonify
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
from google.oauth2.service_account import Credentials

app = Flask(__name__)

GROQ_KEY     = os.environ.get("GROQ_KEY")
WA_TOKEN     = os.environ.get("WA_TOKEN")
WA_PHONE_ID  = os.environ.get("WA_PHONE_ID")
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDENTIALS")

CONTACTS = {
    "CHEF_PROJET" : "2250500277071",
    "PMO"         : "2250555444241",
    "DIRECTEUR"   : "2250506574905"
}

ESCALADE = {
    "FAIBLE"   : [],
    "MOYEN"    : ["CHEF_PROJET"],
    "ELEVE"    : ["CHEF_PROJET", "PMO"],
    "CRITIQUE" : ["CHEF_PROJET", "PMO", "DIRECTEUR"]
}

TYPES_RISQUE_VALIDES = [
    "ACCES", "SECURITE", "TECHNIQUE", "ADMINISTRATIF",
    "METEO", "LOGISTIQUE", "SANITAIRE", "SOCIAL", "AUTRES"
]
SEVERITES_VALIDES = ["CRITIQUE", "ELEVE", "MOYEN", "FAIBLE"]
messages_traites  = set()
groq_client       = Groq(api_key=GROQ_KEY)

LIEN_DASHBOARD = "https://datastudio.google.com/reporting/2edfa803-9950-4350-990a-5a891e438d11"


def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDS)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def sauvegarder_sheets(risque, expediteur, message_original):
    try:
        gc    = get_sheets_client()
        sheet = gc.open("MTN_RAN_Risques").sheet1
        ligne = [
            datetime.now().strftime("%d/%m/%Y %H:%M"),
            expediteur,
            risque.get("projet", "RAN"),
            risque["site_concerne"],
            message_original,
            risque["type_risque"],
            risque["severite"],
            risque["description"],
            risque["action_immediate"],
            "OUI" if risque["bloquer_projet"] else "NON",
            "OPENED"
        ]
        sheet.append_row(ligne)
        print("==> Google Sheets sauvegarde : OK")
    except Exception as e:
        import traceback
        print(f"==> Google Sheets erreur : {e}")
        print(traceback.format_exc())


def recuperer_risques_sheets():
    try:
        gc    = get_sheets_client()
        sheet = gc.open("MTN_RAN_Risques").sheet1
        return sheet.get_all_records()
    except Exception as e:
        print(f"==> Google Sheets lecture erreur : {e}")
        return []


def analyser_risque(message, expediteur):
    projet = "RAN"
    if message.startswith("[FIBRE]") or message.startswith("[FTTH]"):
        projet = "FIBRE"
    elif message.startswith("[5G]"):
        projet = "5G"
    elif message.startswith("[MMONEY]"):
        projet = "MMONEY"

    prompt = f"""Tu es un assistant expert en gestion des risques pour MTN Côte d'Ivoire.
Un technicien terrain vient d'envoyer ce message WhatsApp : "{message}"
Son numéro : {expediteur}
Projet concerné : {projet}

Analyse ce message et réponds UNIQUEMENT en JSON valide avec cette structure exacte :
{{
  "projet": "{projet}",
  "type_risque": "ACCES|SECURITE|TECHNIQUE|ADMINISTRATIF|METEO|LOGISTIQUE|SANITAIRE|SOCIAL|AUTRES",
  "severite": "CRITIQUE|ELEVE|MOYEN|FAIBLE",
  "site_concerne": "nom du site ou INCONNU",
  "description": "résumé clair en 1 phrase",
  "action_immediate": "action recommandée pour le manager",
  "bloquer_projet": true ou false
}}
Ne mets rien d'autre que le JSON dans ta réponse."""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=500
    )
    risque = json.loads(response.choices[0].message.content)

    if risque.get("type_risque") not in TYPES_RISQUE_VALIDES:
        risque["type_risque"] = "AUTRES"
    if risque.get("severite") not in SEVERITES_VALIDES:
        risque["severite"] = "FAIBLE"

    return risque


def envoyer_whatsapp(numero, message):
    url     = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type" : "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to"               : numero,
        "type"             : "text",
        "text"             : {"body": message}
    }
    resp = requests.post(url, headers=headers, json=data)
    print(f"==> WhatsApp envoi à {numero} : {resp.status_code}")


def generer_rapport_hebdo():
    risques = recuperer_risques_sheets()
    prompt  = f"""Tu es un expert en gestion de projet télécoms pour MTN Côte d'Ivoire.
Voici les risques enregistrés cette semaine :
{json.dumps(risques, ensure_ascii=False, indent=2)}

Génère un rapport de gouvernance des risques incluant :
1. Résumé exécutif (3 lignes max)
2. Top 3 risques les plus critiques avec recommandation
3. Tendances détectées
4. Score de santé global du portefeuille (0-100)
5. Décisions urgentes requises cette semaine

Format : texte simple adapté à WhatsApp, sans markdown complexe."""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
    )
    rapport = response.choices[0].message.content
    for role in ["CHEF_PROJET", "PMO", "DIRECTEUR"]:
        envoyer_whatsapp(
            CONTACTS[role],
            f"RAPPORT HEBDO MTN RAN\n\n{rapport}\n\nDashboard : {LIEN_DASHBOARD}"
        )


@app.route("/")
def home():
    return "MTN RAN Risk Platform is live!", 200


@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "mtn_ran_2025":
        return request.args.get("hub.challenge")
    return "Erreur de vérification", 403


@app.route("/webhook", methods=["POST"])
def recevoir_message():
    print("WEBHOOK APPELE VERSION 6.0")
    data = request.json
    print(json.dumps(data, indent=2, ensure_ascii=False))

    try:
        entry = data.get("entry", [])
        if not entry:
            return jsonify({"status": "ok"})

        changes = entry[0].get("changes", [])
        if not changes:
            return jsonify({"status": "ok"})

        value = changes[0].get("value", {})

        if "messages" not in value:
            print("==> Webhook statut ignoré")
            return jsonify({"status": "ok"})

        message_id = value["messages"][0]["id"]
        if message_id in messages_traites:
            print(f"==> Doublon ignoré : {message_id}")
            return jsonify({"status": "ok"})
        messages_traites.add(message_id)

        message    = value["messages"][0]["text"]["body"]
        expediteur = value["messages"][0]["from"]
        print(f"==> Message : {message}")
        print(f"==> Expéditeur : {expediteur}")

        # ============================================
        # COMMANDES SPÉCIALES
        # ============================================
        commande = message.strip().upper()

        if commande in ["RAPPORT", "REPORT", "DASHBOARD", "STATS", "TABLEAU"]:
            envoyer_whatsapp(expediteur,
                f"Tableau de bord MTN RAN Risk Platform :\n\n"
                f"{LIEN_DASHBOARD}\n\n"
                f"Mis à jour en temps réel."
            )
            print("==> Lien dashboard envoyé")
            return jsonify({"status": "ok"})

        if commande in ["AIDE", "HELP", "MENU"]:
            envoyer_whatsapp(expediteur,
                "Commandes disponibles :\n\n"
                "RAPPORT — Recevoir le lien du dashboard\n"
                "AIDE — Afficher ce menu\n\n"
                "Pour signaler un risque, envoyez un message naturel en précisant le projet :\n"
                "[RAN] votre message\n"
                "[FIBRE] votre message\n"
                "[5G] votre message\n"
                "[MMONEY] votre message"
            )
            print("==> Menu d'aide envoyé")
            return jsonify({"status": "ok"})

        # ============================================
        # TRAITEMENT NORMAL — RISQUE TERRAIN
        # ============================================

        # 1. Accusé de réception
        envoyer_whatsapp(expediteur, "Risque reçu. Analyse en cours... Merci.")

        # 2. Analyse IA
        risque = analyser_risque(message, expediteur)
        print(f"==> Risque : {risque}")

        # 3. Confirmation au technicien
        confirmation = (
            f"Risque enregistré :\n"
            f"Projet   : {risque.get('projet', 'RAN')}\n"
            f"Type     : {risque['type_risque']}\n"
            f"Sévérité : {risque['severite']}\n"
            f"Site     : {risque['site_concerne']}\n"
            f"Votre responsable a été alerté.\n\n"
            f"Dashboard : {LIEN_DASHBOARD}"
        )
        envoyer_whatsapp(expediteur, confirmation)

        # 4. Alertes managers selon matrice d'escalade
        destinataires = ESCALADE.get(risque["severite"], [])
        if destinataires:
            alerte = (
                f"ALERTE RISQUE {risque['severite']} — {risque.get('projet','RAN')}\n"
                f"Site     : {risque['site_concerne']}\n"
                f"Signalé  : {expediteur}\n"
                f"Problème : {risque['description']}\n"
                f"Action   : {risque['action_immediate']}\n"
                f"Bloque   : {'OUI' if risque['bloquer_projet'] else 'NON'}\n\n"
                f"Dashboard : {LIEN_DASHBOARD}"
            )
            for role in destinataires:
                envoyer_whatsapp(CONTACTS[role], alerte)

        # 5. Sauvegarde Google Sheets
        sauvegarder_sheets(risque, expediteur, message)
        print("=== TRAITEMENT TERMINÉ AVEC SUCCÈS ===")

    except Exception as e:
        import traceback
        print(f"ERREUR : {e}")
        print(traceback.format_exc())

    return jsonify({"status": "ok"})


scheduler = BackgroundScheduler()
scheduler.add_job(
    generer_rapport_hebdo,
    "cron",
    day_of_week="mon",
    hour=7,
    minute=0
)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
