import os
import json
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ============================================
# CLÉS API — CHARGÉES DEPUIS RENDER
# ============================================
GROQ_KEY      = os.environ.get("GROQ_KEY")
WA_TOKEN      = os.environ.get("WA_TOKEN")
WA_PHONE_ID   = os.environ.get("WA_PHONE_ID")
AIRTABLE_KEY  = os.environ.get("AIRTABLE_KEY")
AIRTABLE_BASE = os.environ.get("AIRTABLE_BASE")

# Numéros WhatsApp des responsables (sans +)
CONTACTS = {
    "CHEF_PROJET" : "2250500277071",
    "PMO"         : "2250555444241",
    "DIRECTEUR"   : "2250506574905"
}

# Matrice d'escalade selon la sévérité
ESCALADE = {
    "FAIBLE"   : [],
    "MOYEN"    : ["CHEF_PROJET"],
    "ELEVE"    : ["CHEF_PROJET", "PMO"],
    "CRITIQUE" : ["CHEF_PROJET", "PMO", "DIRECTEUR"]
}

# Valeurs exactes des champs Single Select Airtable
TYPES_RISQUE_VALIDES = [
    "ACCES", "SECURITE", "TECHNIQUE", "ADMINISTRATIF",
    "METEO", "LOGISTIQUE", "SANITAIRE", "SOCIAL", "AUTRES"
]

SEVERITES_VALIDES = ["CRITIQUE", "ELEVE", "MOYEN", "FAIBLE"]

# ============================================
# CLIENT GROQ (LLAMA DE META)
# ============================================
client = Groq(api_key=GROQ_KEY)

# ============================================
# FONCTIONS PRINCIPALES
# ============================================

def analyser_risque(message, expediteur):
    projet = "RAN"
    if message.startswith("[FIBRE]"):     projet = "FIBRE"
    elif message.startswith("[5G]"):      projet = "5G"
    elif message.startswith("[MMONEY]"):  projet = "MMONEY"

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

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=500
    )
    risque = json.loads(response.choices[0].message.content)

    # Sécurité — forcer des valeurs valides si l'IA répond hors liste
    if risque.get("type_risque") not in TYPES_RISQUE_VALIDES:
        risque["type_risque"] = "AUTRES"
    if risque.get("severite") not in SEVERITES_VALIDES:
        risque["severite"] = "FAIBLE"

    return risque


def envoyer_whatsapp(numero, message):
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
    resp = requests.post(url, headers=headers, json=data)
    print(f"==> WhatsApp envoi à {numero} : {resp.status_code} — {resp.text}")


def sauvegarder_airtable(risque, expediteur, message_original):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Risques"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_KEY}",
        "Content-Type": "application/json"
    }
    data = {"fields": {
        "Date"             : datetime.now().isoformat(),
        "Technicien"       : expediteur,
        "Projet"           : risque.get("projet", "RAN"),
        "Site"             : risque["site_concerne"],
        "Message_original" : message_original,
        "Type"             : risque["type_risque"],       # ACCES|SECURITE|TECHNIQUE|
                                                           # ADMINISTRATIF|METEO|
                                                           # LOGISTIQUE|SANITAIRE|
                                                           # SOCIAL|AUTRES
        "Severite"         : risque["severite"],          # CRITIQUE|ELEVE|MOYEN|FAIBLE
        "Description"      : risque["description"],
        "Action"           : risque["action_immediate"],
        "Bloque"           : risque["bloquer_projet"],
        "Statut"           : "OPENED"                     # OPENED|ONGOING|CLOSED
    }}
    resp = requests.post(url, headers=headers, json=data)
    print(f"==> Airtable sauvegarde : {resp.status_code} — {resp.text}")


def recuperer_risques_airtable(jours=7):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Risques"
    headers = {"Authorization": f"Bearer {AIRTABLE_KEY}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("records", [])
    return []


def generer_rapport_hebdo():
    risques = recuperer_risques_airtable(7)
    prompt = f"""Tu es un expert en gestion de projet télécoms pour MTN Côte d'Ivoire.
Voici les risques enregistrés cette semaine :
{json.dumps(risques, ensure_ascii=False, indent=2)}

Génère un rapport de gouvernance des risques incluant :
1. Résumé exécutif (3 lignes max)
2. Top 3 risques les plus critiques avec recommandation
3. Tendances détectées
4. Score de santé global du portefeuille (0-100)
5. Décisions urgentes requises cette semaine

Format : texte simple adapté à WhatsApp, sans markdown complexe."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
    )
    rapport = response.choices[0].message.content
    for role in ["CHEF_PROJET", "PMO", "DIRECTEUR"]:
        envoyer_whatsapp(CONTACTS[role], f"RAPPORT HEBDO MTN RAN\n\n{rapport}")


# ============================================
# ROUTES FLASK
# ============================================

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
    print("WEBHOOK APPELE VERSION 2.0")
    data = request.json
    print(f"=== MESSAGE RECU ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    try:
        entry = data.get("entry", [])
        print(f"==> Entry trouvé : {len(entry)} élément(s)")

        if not entry:
            print("==> ERREUR : Pas d'entry dans le message")
            return jsonify({"status": "ok"})

        changes = entry[0].get("changes", [])
        print(f"==> Changes trouvés : {len(changes)} élément(s)")

        if not changes:
            print("==> ERREUR : Pas de changes")
            return jsonify({"status": "ok"})

        value = changes[0].get("value", {})
        print(f"==> Value reçue : {value}")

        if "messages" not in value:
            print("==> INFO : Pas de messages (webhook de statut ignoré)")
            return jsonify({"status": "ok"})

        message    = value["messages"][0]["text"]["body"]
        expediteur = value["messages"][0]["from"]
        print(f"==> Message : {message}")
        print(f"==> Expéditeur : {expediteur}")

        # 1. Accusé de réception
        envoyer_whatsapp(expediteur, "Risque reçu. Analyse en cours... Merci.")

        # 2. Analyse IA
        risque = analyser_risque(message, expediteur)
        print(f"==> Risque analysé : {risque}")

        # 3. Confirmation au technicien
        confirmation = (
            f"Risque enregistré :\n"
            f"Projet   : {risque.get('projet', 'RAN')}\n"
            f"Type     : {risque['type_risque']}\n"
            f"Sévérité : {risque['severite']}\n"
            f"Site     : {risque['site_concerne']}\n"
            f"Votre responsable a été alerté."
        )
        envoyer_whatsapp(expediteur, confirmation)

        # 4. Alertes managers
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

        # 5. Sauvegarde Airtable
        sauvegarder_airtable(risque, expediteur, message)
        print("=== TRAITEMENT TERMINÉ AVEC SUCCÈS ===")

    except Exception as e:
        import traceback
        print(f"ERREUR COMPLETE : {e}")
        print(traceback.format_exc())

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
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
