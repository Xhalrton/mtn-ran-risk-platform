from flask import Flask, request, jsonify
from groq import Groq
import requests, json
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ============================================
# VOS CLÉS API — À REMPLACER PAR LES VÔTRES
# ============================================
import os

GROQ_KEY      = os.environ.get("GROQ_KEY")
WA_TOKEN      = os.environ.get("WA_TOKEN")
WA_PHONE_ID   = os.environ.get("WA_PHONE_ID")
AIRTABLE_KEY  = os.environ.get("AIRTABLE_KEY")
AIRTABLE_BASE = os.environ.get("AIRTABLE_BASE")

# Numéros WhatsApp des responsables (avec indicatif pays, sans +)
CONTACTS = {
    "CHEF_PROJET" : "2250700000001",
    "PMO"         : "2250700000002",
    "DIRECTEUR"   : "2250700000003"
}

# Matrice d'escalade selon la sévérité
ESCALADE = {
    "FAIBLE"   : [],
    "MOYEN"    : ["CHEF_PROJET"],
    "ELEVE"    : ["CHEF_PROJET", "PMO"],
    "CRITIQUE" : ["CHEF_PROJET", "PMO", "DIRECTEUR"]
}

# ============================================
# CLIENT GROQ (LLAMA DE META)
# ============================================
client = Groq(api_key=GROQ_KEY)

# ============================================
# FONCTIONS PRINCIPALES
# ============================================

def analyser_risque(message, expediteur):
    """Llama analyse le message terrain et retourne un JSON structuré"""

    # Détection du projet selon le préfixe du message
    projet = "RAN"
    if message.startswith("[FIBRE]"):   projet = "FIBRE"
    elif message.startswith("[5G]"):    projet = "5G"
    elif message.startswith("[MMONEY]"): projet = "MMONEY"

    prompt = f"""Tu es un assistant expert en gestion des risques pour MTN Côte d'Ivoire.
Un technicien terrain vient d'envoyer ce message WhatsApp : "{message}"
Son numéro : {expediteur}
Projet concerné : {projet}

Analyse ce message et réponds UNIQUEMENT en JSON valide avec cette structure exacte :
{{
  "projet": "{projet}",
  "type_risque": "ACCES|SECURITE|TECHNIQUE|ADMINISTRATIF|METEO|AUTRE",
  "severite": "CRITIQUE|ELEVE|MOYEN|FAIBLE",
  "site_concerne": "nom du site ou INCONNU",
  "description": "résumé clair en 1 phrase",
  "action_immediate": "action recommandée pour le manager",
  "bloquer_projet": true ou false
}}
Ne mets rien d'autre que le JSON dans ta réponse."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=500
    )
    return json.loads(response.choices[0].message.content)


def envoyer_whatsapp(numero, message):
    """Envoie un message WhatsApp via Meta Cloud API"""
    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": message}
    }
    requests.post(url, headers=headers, json=data)


def sauvegarder_airtable(risque, expediteur, message_original):
    """Sauvegarde le risque dans Airtable"""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Risques"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_KEY}",
        "Content-Type": "application/json"
    }
    data = {"fields": {
        "Date"             : datetime.now().isoformat(),
        "Technicien"       : expediteur,
        "Message_original" : message_original,
        "Projet"           : risque.get("projet", "RAN"),
        "Type"             : risque["type_risque"],
        "Severite"         : risque["severite"],
        "Site"             : risque["site_concerne"],
        "Description"      : risque["description"],
        "Action"           : risque["action_immediate"],
        "Bloque"           : risque["bloquer_projet"],
        "Statut"           : "OUVERT"
    }}
    requests.post(url, headers=headers, json=data)


def recuperer_risques_airtable(jours=7):
    """Récupère les risques des N derniers jours depuis Airtable"""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Risques"
    headers = {"Authorization": f"Bearer {AIRTABLE_KEY}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("records", [])
    return []


def generer_rapport_hebdo():
    """Génère et envoie le rapport hebdomadaire par Llama chaque lundi à 7h"""
    risques = recuperer_risques_airtable(7)

    prompt = f"""Tu es un expert en gestion de projet télécoms pour MTN Côte d'Ivoire.
Voici les risques enregistrés cette semaine sur les projets ruraux :
{json.dumps(risques, ensure_ascii=False, indent=2)}

Génère un rapport de gouvernance des risques incluant :
1. Résumé exécutif (3 lignes max)
2. Top 3 risques les plus critiques avec recommandation d'action
3. Tendances détectées (types de risques récurrents)
4. Score de santé global du portefeuille (0-100)
5. Décisions urgentes requises cette semaine

Format : texte simple adapté à WhatsApp, sans markdown complexe."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
    )
    rapport = response.choices[0].message.content

    # Envoi à tous les managers
    for role in ["CHEF_PROJET", "PMO", "DIRECTEUR"]:
        envoyer_whatsapp(CONTACTS[role], f"RAPPORT HEBDO MTN RAN\n\n{rapport}")


# ============================================
# ROUTES FLASK (WEBHOOK WHATSAPP)
# ============================================

@app.route("/webhook", methods=["GET"])
def verify():
    """Vérification du webhook par Meta"""
    if request.args.get("hub.verify_token") == "mtn_ran_2025":
        return request.args.get("hub.challenge")
    return "Erreur de vérification", 403


@app.route("/webhook", methods=["POST"])
def recevoir_message():
    """Réception et traitement de chaque message WhatsApp"""
    data = request.json
    try:
        msg = data["entry"][0]["changes"][0]["value"]
        if "messages" in msg:
            message    = msg["messages"][0]["text"]["body"]
            expediteur = msg["messages"][0]["from"]

            # 1. Accuser réception au technicien
            envoyer_whatsapp(expediteur,
                "Risque reçu. Analyse en cours... Merci.")

            # 2. Analyser avec Llama (Groq)
            risque = analyser_risque(message, expediteur)

            # 3. Confirmer au technicien
            confirmation = (
                f"Risque enregistré :\n"
                f"Projet   : {risque.get('projet', 'RAN')}\n"
                f"Type     : {risque['type_risque']}\n"
                f"Sévérité : {risque['severite']}\n"
                f"Site     : {risque['site_concerne']}\n"
                f"Votre responsable a été alerté."
            )
            envoyer_whatsapp(expediteur, confirmation)

            # 4. Alerter les managers selon la matrice d'escalade
            destinataires = ESCALADE.get(risque["severite"], [])
            if destinataires:
                alerte = (
                    f"ALERTE RISQUE {risque['severite']} — {risque.get('projet','RAN')}\n"
                    f"Site     : {risque['site_concerne']}\n"
                    f"Signalé  : {expediteur}\n"
                    f"Problème : {risque['description']}\n"
                    f"Action   : {risque['action_immediate']}\n"
                    f"Bloque   : {'OUI' if risque['bloquer_projet'] else 'NON'}"
                )
                for role in destinataires:
                    envoyer_whatsapp(CONTACTS[role], alerte)

            # 5. Sauvegarder dans Airtable
            sauvegarder_airtable(risque, expediteur, message)

    except Exception as e:
        print(f"Erreur traitement message : {e}")

    return jsonify({"status": "ok"})


# ============================================
# SCHEDULER — RAPPORT AUTOMATIQUE DU LUNDI
# ============================================
scheduler = BackgroundScheduler()
scheduler.add_job(
    generer_rapport_hebdo,
    "cron",
    day_of_week="mon",
    hour=7,
    minute=0
)
scheduler.start()


# ============================================
# DÉMARRAGE
# ============================================
import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello():
    return "Hello, MTN RAN Risk Platform is live!"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Vérification du token
        verify_token = "TON_TOKEN_SECRET"
        if request.args.get("hub.verify_token") == verify_token:
            return request.args.get("hub.challenge")
        return "Token invalide", 403

    elif request.method == "POST":
        data = request.get_json()
        print("Message reçu:", data)
        return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
