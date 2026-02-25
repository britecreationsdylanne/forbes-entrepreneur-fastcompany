"""
CEO Article Generator
Flask application for generating thought leadership articles for Forbes, Entrepreneur, and Fast Company
"""

import os
import json
import uuid
import base64
import tempfile
import secrets
from datetime import datetime
from pathlib import Path
from functools import wraps

import requests as http_requests

from flask import Flask, request, jsonify, send_from_directory, redirect, session, url_for, Response
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__, static_folder='static')
CORS(app)

# Initialize GCS for drafts and saved topics
GCS_BUCKET_NAME = 'ceo-article-generator-drafts'
SAVED_TOPICS_BLOB = 'saved-topics/topics.json'
gcs_client = None
try:
    from google.cloud import storage as gcs_storage
    gcs_client = gcs_storage.Client()
    print("[OK] GCS initialized")
except Exception as e:
    print(f"[WARNING] GCS not available: {e}")

# Fix for running behind Cloud Run's proxy - ensures correct HTTPS URLs
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Session configuration
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

# OAuth configuration
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Allowed email domain
ALLOWED_DOMAIN = 'brite.co'

def login_required(f):
    """Decorator to require authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect('/auth/login')
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Get current user from session"""
    return session.get('user')

# ===================
# Configuration
# ===================

CONFIG_DIR = Path(__file__).parent / 'config'
STYLE_GUIDES_DIR = CONFIG_DIR / 'style_guides'
DRAFTS_DIR = Path(__file__).parent / 'drafts'
DRAFTS_DIR.mkdir(exist_ok=True)

# Google Drive Folder IDs
FOLDER_IDS = {
    'forbes': {
        'drafts': os.environ.get('FORBES_DRAFTS_FOLDER_ID', '162rWBuk5KPdE8501nj7g8j0NRnY0QEzq'),
        'finals': os.environ.get('FORBES_FINALS_FOLDER_ID', '1C1XcfcWMBEtG2hYJIctCc7BmfuoXC0b4')
    },
    'entrepreneur': {
        'drafts': os.environ.get('ENTREPRENEUR_DRAFTS_FOLDER_ID', '1sGwpGc7Hw6irjhgpj_W7-J-t_8AP1wBc'),
        'finals': os.environ.get('ENTREPRENEUR_FINALS_FOLDER_ID', '1mw-Frun3CNi1_4tw4ADB8un5XJRMKepz')
    },
    'fastcompany': {
        'drafts': os.environ.get('FASTCOMPANY_DRAFTS_FOLDER_ID', '1qUnwSzU9RVXMWhxcO-46MdnDCBsrgYPh'),
        'finals': os.environ.get('FASTCOMPANY_FINALS_FOLDER_ID', '1WjQiGYnz9qfWbIsUMVxSeHIiOHJdPsHB')
    }
}

# Email recipients
DRAFT_RECIPIENTS = os.environ.get('DRAFT_RECIPIENTS', 'dylanne.crugnale@brite.co').split(',')
FINAL_RECIPIENTS = os.environ.get('FINAL_RECIPIENTS', 'dylanne.crugnale@brite.co').split(',')

# ClickUp Integration
CLICKUP_API_TOKEN = os.environ.get('CLICKUP_API_TOKEN')
CLICKUP_LIST_ID = os.environ.get('CLICKUP_LIST_ID')

# Todoist Integration
TODOIST_API_TOKEN = os.environ.get('TODOIST_API_TOKEN', '')
TODOIST_PROJECT_ID = os.environ.get('TODOIST_PROJECT_ID', '')

# ===================
# Helper Functions
# ===================

def load_style_guide(publication: str) -> dict:
    """Load style guide JSON for a publication"""
    filename = f"{publication.lower().replace(' ', '')}_style.json"
    filepath = STYLE_GUIDES_DIR / filename
    if filepath.exists():
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def load_brand_guide() -> str:
    """Load BriteCo brand editorial guide (applies to all publications)"""
    filepath = STYLE_GUIDES_DIR.parent / 'briteco_brand_guide.txt'
    if filepath.exists():
        with open(filepath, 'r') as f:
            return f.read()
    return ''

def load_article_examples(publication: str) -> str:
    """Load real published article examples for few-shot prompting"""
    filename = f"{publication.lower().replace(' ', '')}_examples.txt"
    filepath = CONFIG_DIR / 'article_examples' / filename
    if filepath.exists():
        with open(filepath, 'r') as f:
            return f.read()
    return ''

def build_article_system_prompt(publication: str, style_guide: dict, brand_guide: str) -> str:
    """Build the system prompt for article generation (voice, rules, examples)"""
    examples = load_article_examples(publication)

    system_prompt = f"""You are a ghostwriter for Dustin Lemick, CEO and founder of BriteCo, an insurtech company providing specialty jewelry and watch insurance. You write thought leadership articles for {style_guide.get('publication_full_name', publication)}.

YOUR VOICE: First person as Dustin Lemick. Conversational, confident, grounded in real experience. You sound like a founder talking to peers, not a consultant writing a white paper.

BRAND EDITORIAL RULES (follow these strictly for all writing):
{brand_guide}

ANTI-AI WRITING RULES (CRITICAL):
- NEVER use em dashes anywhere. Use commas, periods, colons, or parentheses instead.
- BANNED WORDS/PHRASES: "delve", "landscape", "navigate" (as metaphor), "leverage", "utilize", "pivotal", "crucial", "moreover", "furthermore", "additionally", "indeed", "multifaceted", "tapestry", "unlock potential", "paradigm", "synergy", "holistic", "seamless", "robust", "it's worth noting", "in today's rapidly evolving/changing", "foster", "facilitate", "commences", "harness", "realm", "cutting-edge", "innovative", "comprehensive", "interesting development", "unique situation", "various factors", "It is important to"
- NEVER start paragraphs with "In today's..." or "In an era of..."
- Vary sentence length naturally. Mix short punchy sentences (5-8 words) with medium ones.
- Use contractions naturally (don't, won't, it's, I've, we're).
- Write like a real person talking to a colleague, not like an essay. Include occasional informal transitions ("Look,", "Here's the thing:", "The way I see it,").
- Avoid perfectly parallel structure in lists or consecutive paragraphs. Real writers vary their patterns.
- Don't overuse transitional phrases. Sometimes just start a new thought.
- Prefer simple, everyday words over impressive-sounding ones (use "use" not "utilize", "help" not "facilitate", "start" not "commence").

TONE: {', '.join(style_guide.get('tone', {}).get('primary', ['professional']))}"""

    if examples:
        system_prompt += f"""

REAL PUBLISHED EXAMPLES (match this voice, specificity, and structure closely):
{examples}"""

    return system_prompt

def load_topic_archive() -> dict:
    """Load the topic archive"""
    filepath = CONFIG_DIR / 'topic_archive.json'
    if filepath.exists():
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def get_openai_client():
    """Get OpenAI client"""
    from openai import OpenAI
    return OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

def get_anthropic_client():
    """Get Anthropic client"""
    from anthropic import Anthropic
    return Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

def get_perplexity_client():
    """Get Perplexity client (uses OpenAI SDK)"""
    from openai import OpenAI
    return OpenAI(
        api_key=os.environ.get('PERPLEXITY_API_KEY'),
        base_url="https://api.perplexity.ai"
    )

def sanitize_llm_output(text: str) -> str:
    """Post-process generated text to remove common LLM writing artifacts"""
    import re

    # Replace em dashes (with or without spaces) with comma-based or simpler phrasing
    # " — " or "—" → ", " or " - "
    text = text.replace(' — ', ', ')
    text = text.replace(' —', ',')
    text = text.replace('— ', ', ')
    text = text.replace('—', ', ')

    # Remove overused LLM filler words/phrases (case-insensitive replacements)
    llm_phrases = [
        (r'\bdelve(?:s|d)?\b', 'explore'),
        (r'\bdelving\b', 'exploring'),
        (r'\bIt\'s worth noting that\s*', ''),
        (r'\bIt is worth noting that\s*', ''),
        (r'\bIn today\'s rapidly (?:evolving|changing) (?:landscape|world)\b', 'Today'),
        (r'\bIn today\'s (?:landscape|world|environment|climate)\b', 'Today'),
        (r'\brapidly evolving landscape\b', 'changing market'),
        (r'\bever-evolving landscape\b', 'shifting market'),
        (r'\bever-changing landscape\b', 'shifting market'),
        (r'\bthe landscape of\b', 'the world of'),
        (r'\bnavigate the (?:complex |)landscape\b', 'work through the challenges'),
        (r'\bpivotal\b', 'important'),
        (r'\bcrucial\b', 'important'),
        (r'\bmoreover\b', 'also'),
        (r'\bfurthermore\b', 'also'),
        (r'\badditionally\b', 'also'),
        (r'\bindeed\b', 'really'),
        (r'\bMultifaceted\b', 'Complex'),
        (r'\bmultifaceted\b', 'complex'),
        (r'\btapestry\b', 'mix'),
        (r'\bunlock(?:ing)? the (?:full )?potential\b', 'get the most out'),
        (r'\bparadigm shift\b', 'big change'),
        (r'\bparadigm\b', 'model'),
        (r'\bsynergy\b', 'teamwork'),
        (r'\bholistic\b', 'complete'),
        (r'\bseamless(?:ly)?\b', 'smooth'),
        (r'\bleverage\b', 'use'),
        (r'\bLeverage\b', 'Use'),
        (r'\butilize\b', 'use'),
        (r'\bUtilize\b', 'Use'),
        (r'\bfacilitate\b', 'help with'),
        (r'\bcommence\b', 'start'),
        (r'\brobust\b', 'strong'),
        (r'\bRobust\b', 'Strong'),
    ]

    for pattern, replacement in llm_phrases:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE if pattern[0] != '\\' or 'A-Z' not in pattern else 0)

    # Clean up any double commas or comma-space issues from em dash replacement
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r',\s+,', ',', text)
    # Fix cases where em dash replacement created ", , " or leading commas
    text = re.sub(r'\s,\s(?=[a-z])', ' ', text)

    return text

def get_google_docs_service():
    """Get Google Docs API service"""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get('GOOGLE_DOCS_CREDENTIALS')
    if not creds_json:
        raise ValueError("GOOGLE_DOCS_CREDENTIALS not set")

    creds_data = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_data,
        scopes=[
            'https://www.googleapis.com/auth/documents',
            'https://www.googleapis.com/auth/drive'
        ]
    )

    docs_service = build('docs', 'v1', credentials=credentials)
    drive_service = build('drive', 'v3', credentials=credentials)

    return docs_service, drive_service

# ===================
# Routes - Authentication
# ===================

@app.route('/auth/login')
def auth_login():
    """Initiate Google OAuth login"""
    if get_current_user():
        return redirect('/')
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def auth_callback():
    """Handle Google OAuth callback"""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            return 'Failed to get user info', 400
        email = user_info.get('email', '')
        if not email.endswith(f'@{ALLOWED_DOMAIN}'):
            return f'''
            <html>
            <head><title>Access Denied</title></head>
            <body style="font-family: system-ui; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: linear-gradient(135deg, #018181 0%, #272d3f 100%); color: white;">
                <div style="text-align: center; padding: 40px; background: rgba(0,0,0,0.3); border-radius: 12px;">
                    <h1>Access Denied</h1>
                    <p>Only @{ALLOWED_DOMAIN} email addresses are allowed.</p>
                    <p style="color: #a0aec0;">You signed in with: {email}</p>
                    <a href="/auth/logout" style="color: #00E5E5;">Try a different account</a>
                </div>
            </body>
            </html>
            ''', 403
        session['user'] = {
            'email': email,
            'name': user_info.get('name', ''),
            'picture': user_info.get('picture', '')
        }
        return redirect('/')
    except Exception as e:
        print(f"Auth callback error: {e}")
        return f'Authentication failed: {str(e)}', 400

@app.route('/auth/logout')
def auth_logout():
    """Log out the user"""
    session.pop('user', None)
    return redirect('/auth/login')

@app.route('/api/user')
def get_user():
    """Get current user info"""
    user = get_current_user()
    if user:
        return jsonify(user)
    return jsonify(None), 401

# ===================
# Routes - Static Files
# ===================

@app.route('/')
def index():
    """Serve the main application - redirect to login if not authenticated"""
    user = get_current_user()
    if not user:
        return redirect('/auth/login')

    with open('index.html', 'r', encoding='utf-8') as f:
        html = f.read()

    # Inject user info for the frontend
    user_script = f'''<script>
    window.AUTH_USER = {json.dumps(user)};
    </script>
</head>'''
    html = html.replace('</head>', user_script, 1)

    return Response(html, mimetype='text/html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    return send_from_directory('static', filename)

# ===================
# Routes - Configuration
# ===================

@app.route('/api/config', methods=['GET'])
def get_config():
    """Get application configuration"""
    return jsonify({
        'publications': [
            {'id': 'forbes', 'name': 'Forbes', 'full_name': 'Forbes Business Council'},
            {'id': 'entrepreneur', 'name': 'Entrepreneur', 'full_name': 'Entrepreneur Leadership Network'},
            {'id': 'fastcompany', 'name': 'Fast Company', 'full_name': 'Fast Company Executive Board'}
        ],
        'months': [
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December'
        ],
        'current_year': datetime.now().year
    })

@app.route('/api/style-guide/<publication>', methods=['GET'])
def get_style_guide(publication):
    """Get style guide for a publication"""
    style_guide = load_style_guide(publication)
    if not style_guide:
        return jsonify({'error': 'Style guide not found'}), 404
    return jsonify(style_guide)

@app.route('/api/topic-archive', methods=['GET'])
def get_topic_archive():
    """Get the topic archive"""
    archive = load_topic_archive()
    return jsonify(archive)

@app.route('/api/topic-archive/<publication>', methods=['GET'])
def get_publication_archive(publication):
    """Get topic archive for a specific publication"""
    archive = load_topic_archive()
    # Map frontend IDs to archive keys
    pub_key_map = {
        'forbes': 'forbes',
        'entrepreneur': 'entrepreneur',
        'fastcompany': 'fast_company'
    }
    pub_key = pub_key_map.get(publication.lower(), publication.lower())
    if pub_key in archive:
        return jsonify(archive[pub_key])
    return jsonify({'error': 'Publication not found', 'tried_key': pub_key}), 404

# ===================
# Routes - Topic Research & Generation
# ===================

@app.route('/api/research-topics', methods=['POST'])
def research_topics():
    """Research current trends for topic generation"""
    data = request.json
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)

    try:
        # Use Perplexity for research
        client = get_perplexity_client()

        # Build research query
        query = f"""
        Find current business and leadership trends, news, and topics for {month} {year} that would be relevant for a thought leadership article.
        Focus on:
        - Business strategy and leadership
        - Entrepreneurship and startups
        - Workplace culture and hiring
        - Technology and innovation
        - Economic trends affecting businesses

        Provide 5-7 trending topics with brief descriptions of why they're relevant right now.
        """

        response = client.chat.completions.create(
            model="sonar",
            messages=[
                {"role": "system", "content": "You are a business trend researcher. Provide current, timely topics for thought leadership articles."},
                {"role": "user", "content": query}
            ],
            max_tokens=1500
        )

        research_results = response.choices[0].message.content

        return jsonify({
            'success': True,
            'research': research_results,
            'publication': publication,
            'month': month,
            'year': year
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate-topics', methods=['POST'])
def generate_topics():
    """Generate 10 topic ideas based on research and style guide"""
    data = request.json
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)
    research = data.get('research', '')

    try:
        # Load style guide and archive
        style_guide = load_style_guide(publication)
        archive = load_topic_archive()

        # Get past topics to avoid
        pub_key_map = {
            'forbes': 'forbes',
            'entrepreneur': 'entrepreneur',
            'fastcompany': 'fast_company'
        }
        pub_key = pub_key_map.get(publication.lower(), publication.lower())
        past_topics = []
        if pub_key in archive:
            past_topics = archive[pub_key].get('topics_to_avoid', [])

        # Use Claude for topic generation
        client = get_anthropic_client()

        prompt = f"""
        Generate 10 unique topic ideas for a {publication} thought leadership article for {month} {year}.

        PUBLICATION STYLE:
        - Publication: {style_guide.get('publication_full_name', publication)}
        - Typical word count: {style_guide.get('specifications', {}).get('word_count', {}).get('min', 700)}-{style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words
        - Tone: {', '.join(style_guide.get('tone', {}).get('primary', ['professional']))}
        - Author: Dustin Lemick, CEO of BriteCo (jewelry/watch insurance, insurtech)

        HEADLINE PATTERNS TO USE:
        {json.dumps(style_guide.get('headline_patterns', []), indent=2)}

        CURRENT RESEARCH/TRENDS:
        {research}

        TOPICS TO AVOID (already written):
        {', '.join(past_topics)}

        For each topic, provide:
        1. A headline in the publication's style
        2. A one-sentence angle/hook
        3. Why it's timely for {month} {year}
        4. How Dustin/BriteCo could connect to this topic

        Return as JSON array with 10 objects, each having: headline, angle, timeliness, briteco_connection
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=3000,
            temperature=0.6,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Parse response
        response_text = response.content[0].text

        # Try to extract JSON from response
        try:
            # Find JSON array in response
            start_idx = response_text.find('[')
            end_idx = response_text.rfind(']') + 1
            if start_idx != -1 and end_idx > start_idx:
                json_str = response_text[start_idx:end_idx]
                topics = json.loads(json_str)
            else:
                topics = []
        except json.JSONDecodeError:
            topics = [{"headline": "Error parsing topics", "angle": response_text, "timeliness": "", "briteco_connection": ""}]

        return jsonify({
            'success': True,
            'topics': topics,
            'publication': publication,
            'month': month,
            'year': year
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/refine-topic', methods=['POST'])
def refine_topic():
    """Refine a custom topic using AI to make it publication-ready"""
    data = request.json
    publication = data.get('publication')
    headline = data.get('headline', '')
    angle = data.get('angle', '')

    try:
        # Load style guide
        style_guide = load_style_guide(publication)

        # Use Claude to refine the topic
        client = get_anthropic_client()

        prompt = f"""
        Refine this custom topic idea into a polished {publication} article topic.

        USER'S ROUGH IDEA:
        Headline: {headline}
        Description/Angle: {angle}

        PUBLICATION STYLE:
        - Publication: {style_guide.get('publication_full_name', publication)}
        - Headline patterns: {json.dumps(style_guide.get('headline_patterns', []), indent=2)}
        - Tone: {', '.join(style_guide.get('tone', {}).get('primary', ['professional']))}
        - Author: Dustin Lemick, CEO of BriteCo (jewelry/watch insurance, insurtech)

        Please refine this into:
        1. A polished headline that matches the publication's style and patterns
        2. A clear, compelling angle (1-2 sentences)
        3. Why this is timely and relevant
        4. How Dustin/BriteCo could naturally connect to this topic

        Return as JSON object with: headline, angle, timeliness, briteco_connection
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1000,
            temperature=0.5,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Parse response
        response_text = response.content[0].text

        # Try to extract JSON from response
        try:
            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1
            if start_idx != -1 and end_idx > start_idx:
                json_str = response_text[start_idx:end_idx]
                topic = json.loads(json_str)
            else:
                topic = {"headline": headline, "angle": angle, "timeliness": "Custom topic", "briteco_connection": ""}
        except json.JSONDecodeError:
            topic = {"headline": headline, "angle": angle, "timeliness": "Custom topic", "briteco_connection": ""}

        return jsonify({
            'success': True,
            'topic': topic
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate-variations', methods=['POST'])
def generate_variations():
    """Generate 10 topic variations based on a selected topic"""
    data = request.json
    publication = data.get('publication')
    topic = data.get('topic', {})

    try:
        style_guide = load_style_guide(publication)
        client = get_anthropic_client()

        prompt = f"""
        A CEO liked this topic idea for a {publication} article but wants to explore variations on the same theme.

        ORIGINAL TOPIC:
        Headline: {topic.get('headline', '')}
        Angle: {topic.get('angle', '')}
        Timeliness: {topic.get('timeliness', '')}
        BriteCo Connection: {topic.get('briteco_connection', '')}

        PUBLICATION STYLE:
        - Publication: {style_guide.get('publication_full_name', publication)}
        - Headline patterns: {json.dumps(style_guide.get('headline_patterns', []), indent=2)}
        - Tone: {', '.join(style_guide.get('tone', {}).get('primary', ['professional']))}
        - Author: Dustin Lemick, CEO of BriteCo (jewelry/watch insurance, insurtech)

        Generate 10 NEW topic variations that explore the SAME general theme but with different angles, perspectives, or hooks.
        Each should feel distinct while staying in the same subject area. Mix up the approaches: some more data-driven, some more narrative, some contrarian, some forward-looking.

        For each topic, provide:
        1. A headline in the publication's style
        2. A one-sentence angle/hook
        3. Why it's timely and relevant
        4. How Dustin/BriteCo could connect to this topic

        Return as JSON array with 10 objects, each having: headline, angle, timeliness, briteco_connection
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=3000,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = response.content[0].text

        try:
            start_idx = response_text.find('[')
            end_idx = response_text.rfind(']') + 1
            if start_idx != -1 and end_idx > start_idx:
                topics = json.loads(response_text[start_idx:end_idx])
            else:
                topics = []
        except json.JSONDecodeError:
            topics = []

        return jsonify({
            'success': True,
            'topics': topics,
            'original_headline': topic.get('headline', '')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate-talking-points', methods=['POST'])
def generate_talking_points():
    """Generate AI talking points and thought questions for a selected topic"""
    data = request.json
    publication = data.get('publication')
    topic = data.get('topic', {})

    try:
        style_guide = load_style_guide(publication)
        client = get_anthropic_client()

        prompt = f"""
        Generate 5-7 talking points and thought-provoking questions for a CEO who is about to record their thoughts on this topic for a {publication} article.

        TOPIC:
        Headline: {topic.get('headline', '')}
        Angle: {topic.get('angle', '')}
        BriteCo Connection: {topic.get('briteco_connection', '')}

        PUBLICATION: {style_guide.get('publication_full_name', publication)}
        AUTHOR: Dustin Lemick, CEO of BriteCo (jewelry/watch insurance, insurtech)

        Generate a mix of:
        - Key talking points to cover (things the CEO should address)
        - Thought-provoking questions to spark ideas (starting with "What...", "How...", "When...")
        - A prompt about a personal story or BriteCo example they could share

        Return as a JSON array of objects, each with:
        - "type": either "talking_point" or "question"
        - "text": the talking point or question text

        Keep each item concise (1-2 sentences max). Focus on substance, not platitudes.
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1500,
            temperature=0.6,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = response.content[0].text

        try:
            start_idx = response_text.find('[')
            end_idx = response_text.rfind(']') + 1
            if start_idx != -1 and end_idx > start_idx:
                points = json.loads(response_text[start_idx:end_idx])
            else:
                points = []
        except json.JSONDecodeError:
            points = [{"type": "talking_point", "text": response_text}]

        return jsonify({
            'success': True,
            'talking_points': points,
            'topic': topic.get('headline', '')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate-inspiration', methods=['POST'])
def generate_inspiration():
    """Generate detailed article brief / inspiration for the CEO before recording"""
    data = request.json
    publication = data.get('publication')
    topic = data.get('topic', {})

    try:
        style_guide = load_style_guide(publication)
        brand_guide = load_brand_guide()
        client = get_anthropic_client()

        prompt = f"""
        You are helping a CEO prepare to record his thoughts for a {style_guide.get('publication_full_name', publication)} thought leadership article.

        TOPIC:
        Headline: {topic.get('headline', '')}
        Angle: {topic.get('angle', '')}
        Why it's timely: {topic.get('timeliness', '')}
        BriteCo Connection: {topic.get('briteco_connection', '')}

        AUTHOR: Dustin Lemick, CEO of BriteCo, an insurtech company providing specialty jewelry and watch insurance.
        PUBLICATION TONE: {', '.join(style_guide.get('tone', {}).get('primary', ['professional']))}
        TARGET WORD COUNT: {style_guide.get('specifications', {}).get('word_count', {}).get('min', 700)}-{style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words

        Generate a detailed article brief to inspire and guide the CEO before recording. Format as HTML with the following sections:

        1. <h4>Article Vision</h4>: A 2-3 sentence summary of what this article should accomplish and the reader takeaway.

        2. <h4>Suggested Structure</h4>: An outline with:
           - Opening hook idea (1-2 specific options)
           - 3-4 section ideas with brief descriptions of what each should cover
           - Closing approach

        3. <h4>Key Points to Cover</h4>: A bulleted list of 5-6 specific points, data angles, or arguments the article should make.

        4. <h4>BriteCo Stories & Examples</h4>: 3-4 specific prompts for personal stories, BriteCo experiences, or industry examples Dustin could share. Be specific and reference things a jewelry insurtech CEO would actually experience.

        5. <h4>Data & References to Consider</h4>: 3-4 types of statistics, studies, or expert perspectives that would strengthen the article. Suggest what to look up or mention.

        Return ONLY the HTML content (no markdown, no code blocks). Use <h4> for section headers, <ul><li> for lists, <p> for paragraphs, and <strong> for emphasis.
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=2000,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )

        return jsonify({
            'success': True,
            'inspiration': response.content[0].text
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================
# Routes - Audio Recording & Transcription
# ===================

@app.route('/api/transcribe', methods=['POST'])
def transcribe_audio():
    """Transcribe audio file using Whisper API"""
    try:
        # Check if file is in request
        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400

        audio_file = request.files['audio']

        # Preserve original file extension so Whisper can detect format
        original_ext = os.path.splitext(audio_file.filename or '')[1] or '.webm'
        with tempfile.NamedTemporaryFile(delete=False, suffix=original_ext) as tmp:
            audio_file.save(tmp.name)
            tmp_path = tmp.name

        try:
            # Transcribe with Whisper
            client = get_openai_client()

            with open(tmp_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text"
                )

            return jsonify({
                'success': True,
                'transcription': transcript
            })

        finally:
            # Clean up temp file
            os.unlink(tmp_path)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================
# Routes - Article Generation
# ===================

@app.route('/api/generate-article', methods=['POST'])
def generate_article():
    """Generate article from transcription using style guide"""
    data = request.json
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)
    topic = data.get('topic', {})
    transcription = data.get('transcription', '')

    try:
        # Load style guide and brand guide
        style_guide = load_style_guide(publication)
        brand_guide = load_brand_guide()

        # Use Claude for article generation
        client = get_anthropic_client()

        # Build system prompt (voice, rules, examples)
        system_prompt = build_article_system_prompt(publication, style_guide, brand_guide)

        # Build user prompt (specific task)
        prompt = f"""Write a thought leadership article for {style_guide.get('publication_full_name', publication)}.

        TOPIC:
        Headline: {topic.get('headline', 'Untitled')}
        Angle: {topic.get('angle', '')}

        CEO'S THOUGHTS (from recording transcription):
        {transcription}

        REQUIREMENTS:
        - Word count: {style_guide.get('specifications', {}).get('word_count', {}).get('min', 700)}-{style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words (strict, do not exceed {style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words)
        - Subheading format: {"ALL CAPS" if publication.lower() == 'fastcompany' else "Sentence case phrases"}
        {"- Do NOT include Key Takeaways bullets — Entrepreneur editors add those on their end." if publication.lower() == 'entrepreneur' else ""}
        {"- PUNCTUATION OVERRIDE: Do NOT use the serial comma for this publication (e.g., 'apples, oranges and bananas' NOT 'apples, oranges, and bananas'). Do NOT link to Forbes, Fast Company, or Inc. (competitors)." if publication.lower() == 'entrepreneur' else ""}

        STRUCTURE:
        {json.dumps(style_guide.get('article_formats', [{}])[0].get('structure', {}), indent=2)}

        BRITECO INTEGRATION:
        - Weave in 2-4 references to BriteCo, Dustin's experience, or the jewelry/insurance industry
        - Use natural connections, not forced mentions

        SAMPLE SUBHEADINGS FROM THIS PUBLICATION:
        {json.dumps(style_guide.get('subheading_patterns', {}).get('examples', [])[:5], indent=2)}

        Use specific, concrete details from the transcription rather than generic business platitudes.
        Write the complete article now. Make it engaging, insightful, and true to the CEO's voice from the transcription.
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=4000,
            temperature=0.7,
            system=system_prompt,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        article_content = response.content[0].text

        # Post-process to catch any remaining LLM artifacts
        article_content = sanitize_llm_output(article_content)

        # Count words
        word_count = len(article_content.split())

        return jsonify({
            'success': True,
            'article': article_content,
            'word_count': word_count,
            'publication': publication,
            'topic': topic,
            'month': month,
            'year': year
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/rewrite-article', methods=['POST'])
def rewrite_article():
    """Rewrite/improve an article section"""
    data = request.json
    publication = data.get('publication')
    article = data.get('article')
    instructions = data.get('instructions', 'Improve this article')

    try:
        style_guide = load_style_guide(publication)
        brand_guide = load_brand_guide()
        client = get_anthropic_client()

        # Build system prompt (voice, rules, examples)
        system_prompt = build_article_system_prompt(publication, style_guide, brand_guide)

        prompt = f"""Rewrite/improve this {publication} article based on these instructions:

        INSTRUCTIONS: {instructions}

        CURRENT ARTICLE:
        {article}

        REQUIREMENTS:
        - Maintain the {publication} style and format
        - Keep subheadings in {"ALL CAPS" if publication.lower() == 'fastcompany' else "sentence case"}
        - Target word count: {style_guide.get('specifications', {}).get('word_count', {}).get('min', 700)}-{style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words (strict, do not exceed {style_guide.get('specifications', {}).get('word_count', {}).get('max', 800)} words)
        {"- PUNCTUATION OVERRIDE: Do NOT use the serial comma for this publication (e.g., 'apples, oranges and bananas' NOT 'apples, oranges, and bananas'). Do NOT link to Forbes, Fast Company, or Inc. (competitors)." if publication.lower() == 'entrepreneur' else ""}

        Provide the complete rewritten article.
        """

        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=4000,
            temperature=0.7,
            system=system_prompt,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        rewritten = sanitize_llm_output(response.content[0].text)

        return jsonify({
            'success': True,
            'article': rewritten,
            'word_count': len(rewritten.split())
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================
# Routes - Draft Management (GCS)
# ===================

@app.route('/api/drafts/save', methods=['POST'])
def save_draft():
    """Save a draft to GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503

    try:
        data = request.json
        draft_id = data.get('draft_id') or str(uuid.uuid4())

        # Get current user for creator tracking (session first, frontend fallback)
        current_user = get_current_user()
        user_email = current_user.get('email', 'Unknown') if current_user else data.get('user_email', 'Unknown')

        # Check if draft already exists to preserve created_at and created_by
        existing_draft = {}
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob_name = f"drafts/{draft_id}.json"
        blob = bucket.blob(blob_name)

        if blob.exists():
            existing_draft = json.loads(blob.download_as_text())

        draft = {
            'id': draft_id,
            'publication': data.get('publication'),
            'month': data.get('month'),
            'year': data.get('year'),
            'current_step': data.get('current_step', 1),
            'data': data.get('data', {}),
            'created_at': existing_draft.get('created_at', datetime.now().isoformat()),
            'created_by': existing_draft.get('created_by', user_email),
            'updated_at': datetime.now().isoformat()
        }

        # Save to GCS
        blob.upload_from_string(json.dumps(draft, indent=2), content_type='application/json')

        return jsonify({
            'success': True,
            'draft_id': draft_id,
            'message': 'Draft saved successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/drafts/list', methods=['GET'])
def list_drafts():
    """List all drafts from GCS"""
    if not gcs_client:
        return jsonify({'drafts': []})

    try:
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix='drafts/'))
        drafts = []

        for blob in blobs:
            if blob.name.endswith('.json'):
                try:
                    draft = json.loads(blob.download_as_text())
                    drafts.append({
                        'id': draft.get('id'),
                        'publication': draft.get('publication'),
                        'month': draft.get('month'),
                        'year': draft.get('year'),
                        'current_step': draft.get('current_step'),
                        'title': draft.get('data', {}).get('topic', {}).get('headline', 'Untitled'),
                        'created_at': draft.get('created_at'),
                        'created_by': draft.get('created_by', 'Unknown'),
                        'updated_at': draft.get('updated_at')
                    })
                except Exception:
                    continue

        # Sort by updated_at descending
        drafts.sort(key=lambda x: x.get('updated_at', ''), reverse=True)

        return jsonify({'drafts': drafts})

    except Exception as e:
        return jsonify({'drafts': [], 'error': str(e)})

@app.route('/api/drafts/<draft_id>', methods=['GET'])
def get_draft(draft_id):
    """Get a specific draft from GCS"""
    if not gcs_client:
        return jsonify({'error': 'GCS not available'}), 503

    try:
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"drafts/{draft_id}.json")

        if not blob.exists():
            return jsonify({'error': 'Draft not found'}), 404

        draft = json.loads(blob.download_as_text())
        return jsonify(draft)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/drafts/<draft_id>', methods=['DELETE'])
def delete_draft(draft_id):
    """Delete a draft from GCS"""
    if not gcs_client:
        return jsonify({'success': True})

    try:
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"drafts/{draft_id}.json")

        if blob.exists():
            blob.delete()

        return jsonify({'success': True, 'message': 'Draft deleted'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/complete', methods=['POST'])
def complete_project():
    """Move a draft to completed status in GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503

    try:
        data = request.json
        draft_id = data.get('draft_id')
        if not draft_id:
            return jsonify({'success': False, 'error': 'draft_id required'}), 400

        current_user = get_current_user()
        user_email = current_user.get('email', 'Unknown') if current_user else 'Unknown'

        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        source_blob = bucket.blob(f"drafts/{draft_id}.json")

        if not source_blob.exists():
            return jsonify({'success': False, 'error': 'Draft not found'}), 404

        # Read existing draft data
        draft = json.loads(source_blob.download_as_text())

        # Add completion metadata
        draft['completed_at'] = datetime.now().isoformat()
        draft['completed_by'] = user_email

        # Write to completed/ prefix
        dest_blob = bucket.blob(f"completed/{draft_id}.json")
        dest_blob.upload_from_string(
            json.dumps(draft, indent=2),
            content_type='application/json'
        )

        # Delete from drafts/
        source_blob.delete()

        return jsonify({
            'success': True,
            'message': 'Project completed successfully',
            'draft_id': draft_id
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/completed/list', methods=['GET'])
def list_completed():
    """List completed articles from GCS, optionally filtered by publication"""
    if not gcs_client:
        return jsonify({'completed': []})

    try:
        publication_filter = request.args.get('publication')

        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix='completed/'))
        completed = []

        for blob in blobs:
            if blob.name.endswith('.json'):
                try:
                    article = json.loads(blob.download_as_text())

                    if publication_filter and article.get('publication') != publication_filter:
                        continue

                    completed.append({
                        'id': article.get('id'),
                        'publication': article.get('publication'),
                        'month': article.get('month'),
                        'year': article.get('year'),
                        'title': article.get('data', {}).get('topic', {}).get('headline', 'Untitled'),
                        'created_at': article.get('created_at'),
                        'created_by': article.get('created_by', 'Unknown'),
                        'completed_at': article.get('completed_at'),
                        'completed_by': article.get('completed_by', 'Unknown')
                    })
                except Exception:
                    continue

        completed.sort(key=lambda x: x.get('completed_at', ''), reverse=True)

        return jsonify({'completed': completed})

    except Exception as e:
        return jsonify({'completed': [], 'error': str(e)})

# ===================
# Routes - Topic Choice Logging (GCS)
# ===================

@app.route('/api/log-topic-choice', methods=['POST'])
def log_topic_choice():
    """Log a topic selection to GCS for future algorithm improvement"""
    if not gcs_client:
        return jsonify({'success': True})  # Silently skip if GCS unavailable

    try:
        data = request.json
        current_user = get_current_user()
        user_email = current_user.get('email', 'Unknown') if current_user else data.get('user_email', 'Unknown')

        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'user': user_email,
            'publication': data.get('publication'),
            'month': data.get('month'),
            'year': data.get('year'),
            'action': data.get('action', 'select'),  # select, generate_variations, use_saved
            'selected_topic': data.get('selected_topic'),
            'all_topics_shown': data.get('all_topics', []),
            'was_variation': data.get('was_variation', False),
            'original_topic': data.get('original_topic')
        }

        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob_name = f"topic-logs/{datetime.now().strftime('%Y-%m')}/{uuid.uuid4()}.json"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(json.dumps(log_entry, indent=2), content_type='application/json')

        return jsonify({'success': True})

    except Exception:
        return jsonify({'success': True})  # Never fail the user flow for logging

# ===================
# Routes - Saved Topics (GCS) - Organized by Publication
# ===================

@app.route('/api/saved-topics/<publication>', methods=['GET'])
def list_saved_topics(publication):
    """List saved topics for a specific publication from GCS"""
    if not gcs_client:
        return jsonify({'success': True, 'topics': []})

    try:
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(SAVED_TOPICS_BLOB)

        if not blob.exists():
            return jsonify({'success': True, 'topics': []})

        all_topics = json.loads(blob.download_as_text())
        # Return topics for specific publication
        pub_topics = all_topics.get(publication.lower(), [])
        return jsonify({'success': True, 'topics': pub_topics})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'topics': []})

@app.route('/api/saved-topics/<publication>', methods=['POST'])
def save_topic(publication):
    """Save a topic for a specific publication"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503

    try:
        topic = request.json
        if not topic or not topic.get('headline'):
            return jsonify({'success': False, 'error': 'Topic headline required'}), 400

        # Get current user
        current_user = get_current_user()
        user_email = current_user.get('email', 'Unknown') if current_user else 'Unknown'

        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(SAVED_TOPICS_BLOB)

        # Load existing topics (organized by publication)
        all_topics = {}
        if blob.exists():
            all_topics = json.loads(blob.download_as_text())

        pub_key = publication.lower()
        if pub_key not in all_topics:
            all_topics[pub_key] = []

        # Check if already saved (by headline)
        if any(t.get('headline') == topic.get('headline') for t in all_topics[pub_key]):
            return jsonify({'success': False, 'error': 'Topic already saved'})

        # Add metadata
        topic['savedAt'] = datetime.now().isoformat()
        topic['savedBy'] = user_email
        topic['publication'] = pub_key

        all_topics[pub_key].append(topic)
        blob.upload_from_string(json.dumps(all_topics, indent=2), content_type='application/json')

        return jsonify({'success': True, 'topic': topic})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/saved-topics/<publication>/<int:index>', methods=['DELETE'])
def delete_saved_topic(publication, index):
    """Delete a saved topic by publication and index"""
    if not gcs_client:
        return jsonify({'success': True})

    try:
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(SAVED_TOPICS_BLOB)

        if not blob.exists():
            return jsonify({'success': True})

        all_topics = json.loads(blob.download_as_text())
        pub_key = publication.lower()

        if pub_key in all_topics and 0 <= index < len(all_topics[pub_key]):
            all_topics[pub_key].pop(index)
            blob.upload_from_string(json.dumps(all_topics, indent=2), content_type='application/json')

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ===================
# Routes - Google Docs Export
# ===================

import re
from html.parser import HTMLParser

class HTMLToDocsParser(HTMLParser):
    """Parse HTML and convert to Google Docs formatting requests"""

    def __init__(self):
        super().__init__()
        self.text = ""
        self.formatting_ranges = []
        self.current_pos = 0
        self.tag_stack = []
        self.link_url = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag in ['strong', 'b']:
            self.tag_stack.append(('bold', len(self.text)))
        elif tag in ['em', 'i']:
            self.tag_stack.append(('italic', len(self.text)))
        elif tag == 'u':
            self.tag_stack.append(('underline', len(self.text)))
        elif tag == 'a':
            self.link_url = attrs_dict.get('href', '')
            self.tag_stack.append(('link', len(self.text)))
        elif tag == 'h2':
            self.tag_stack.append(('heading2', len(self.text)))
        elif tag == 'h3':
            self.tag_stack.append(('heading3', len(self.text)))
        elif tag == 'br':
            self.text += '\n'
        elif tag == 'p':
            if self.text and not self.text.endswith('\n'):
                self.text += '\n'
        elif tag == 'blockquote':
            self.tag_stack.append(('blockquote', len(self.text)))

    def handle_endtag(self, tag):
        if tag in ['strong', 'b']:
            self._close_tag('bold')
        elif tag in ['em', 'i']:
            self._close_tag('italic')
        elif tag == 'u':
            self._close_tag('underline')
        elif tag == 'a':
            self._close_tag('link')
            self.link_url = None
        elif tag == 'h2':
            self._close_tag('heading2')
            if not self.text.endswith('\n'):
                self.text += '\n'
        elif tag == 'h3':
            self._close_tag('heading3')
            if not self.text.endswith('\n'):
                self.text += '\n'
        elif tag in ['p', 'div']:
            if not self.text.endswith('\n'):
                self.text += '\n'
        elif tag == 'blockquote':
            self._close_tag('blockquote')
            if not self.text.endswith('\n'):
                self.text += '\n'

    def _close_tag(self, tag_type):
        for i in range(len(self.tag_stack) - 1, -1, -1):
            if self.tag_stack[i][0] == tag_type:
                _, start = self.tag_stack.pop(i)
                end = len(self.text)
                if end > start:
                    self.formatting_ranges.append({
                        'type': tag_type,
                        'start': start,
                        'end': end,
                        'url': self.link_url if tag_type == 'link' else None
                    })
                break

    def handle_data(self, data):
        self.text += data

    def get_docs_requests(self, start_index=1):
        """Convert formatting ranges to Google Docs API requests"""
        requests = []

        for fmt in self.formatting_ranges:
            start = start_index + fmt['start']
            end = start_index + fmt['end']

            if fmt['type'] == 'bold':
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {'bold': True},
                        'fields': 'bold'
                    }
                })
            elif fmt['type'] == 'italic':
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {'italic': True},
                        'fields': 'italic'
                    }
                })
            elif fmt['type'] == 'underline':
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {'underline': True},
                        'fields': 'underline'
                    }
                })
            elif fmt['type'] == 'link' and fmt['url']:
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {
                            'link': {'url': fmt['url']},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0, 'green': 0.5, 'blue': 0.5}}}
                        },
                        'fields': 'link,foregroundColor'
                    }
                })
            elif fmt['type'] == 'heading2':
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {'bold': True, 'fontSize': {'magnitude': 16, 'unit': 'PT'}},
                        'fields': 'bold,fontSize'
                    }
                })
            elif fmt['type'] == 'heading3':
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': start, 'endIndex': end},
                        'textStyle': {'bold': True, 'fontSize': {'magnitude': 14, 'unit': 'PT'}},
                        'fields': 'bold,fontSize'
                    }
                })

        return requests

def parse_html_for_docs(html_content):
    """Parse HTML content and return text + formatting requests"""
    if not html_content:
        return None, []

    parser = HTMLToDocsParser()
    try:
        parser.feed(html_content)
        return parser.text, parser
    except Exception as e:
        print(f"HTML parsing error: {e}")
        return None, None

def get_pub_display_name(pub_key):
    """Get display name for publication"""
    names = {
        'forbes': 'Forbes',
        'entrepreneur': 'Entrepreneur',
        'fastcompany': 'Fast Company'
    }
    return names.get(pub_key.lower(), pub_key)

def format_doc_title(year, month, publication, doc_type):
    """Format document title: Year Month Publication Type"""
    pub_name = get_pub_display_name(publication)
    return f"{year} {month} {pub_name} {doc_type}"

@app.route('/api/export-transcription', methods=['POST'])
def export_transcription():
    """Export transcription to Google Docs"""
    data = request.json
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)
    transcription = data.get('transcription')
    topic = data.get('topic', {})

    if not transcription:
        return jsonify({'error': 'No transcription provided'}), 400

    try:
        docs_service, drive_service = get_google_docs_service()

        # Use drafts folder for transcriptions
        pub_key = publication.lower().replace(' ', '')
        folder_id = FOLDER_IDS.get(pub_key, {}).get('drafts')

        if not folder_id:
            return jsonify({'error': 'Folder ID not configured'}), 400

        # Verify folder access first (supports Shared Drives)
        try:
            folder_check = drive_service.files().get(
                fileId=folder_id,
                fields='id, name',
                supportsAllDrives=True
            ).execute()
            print(f"[API] Folder access OK: {folder_check.get('name')}")
        except Exception as folder_err:
            return jsonify({
                'error': f"Cannot access Google Drive folder. Ensure the service account has access. Folder ID: {folder_id}. Error: {str(folder_err)}"
            }), 500

        # Format title: Year Month Publication Transcribed Audio
        title = format_doc_title(year, month, publication, 'Transcribed Audio')

        # Build document content
        content = f"TOPIC: {topic.get('headline', 'Untitled')}\n\n"
        content += f"ANGLE: {topic.get('angle', '')}\n\n"
        content += "=" * 50 + "\n"
        content += "TRANSCRIPTION\n"
        content += "=" * 50 + "\n\n"
        content += transcription

        # Create the document (supports Shared Drives)
        doc_metadata = {
            'name': title,
            'mimeType': 'application/vnd.google-apps.document',
            'parents': [folder_id]
        }

        doc = drive_service.files().create(
            body=doc_metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()
        doc_id = doc.get('id')

        # Add content to document
        requests_list = [
            {
                'insertText': {
                    'location': {'index': 1},
                    'text': content
                }
            }
        ]

        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': requests_list}
        ).execute()

        # Make document accessible (supports Shared Drives)
        drive_service.permissions().create(
            fileId=doc_id,
            body={'type': 'anyone', 'role': 'reader'},
            supportsAllDrives=True
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

        return jsonify({
            'success': True,
            'doc_id': doc_id,
            'doc_url': doc_url,
            'title': title
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export-to-docs', methods=['POST'])
def export_to_docs():
    """Export article to Google Docs with formatting preserved"""
    data = request.json
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)
    article = data.get('article')
    article_html = data.get('article_html')  # HTML version with formatting
    is_final = data.get('is_final', False)

    # Format title: Year Month Publication Draft/Final
    doc_type = 'Final' if is_final else 'Draft'
    title = format_doc_title(year, month, publication, doc_type)

    try:
        docs_service, drive_service = get_google_docs_service()

        # Determine folder
        pub_key = publication.lower().replace(' ', '')
        folder_type = 'finals' if is_final else 'drafts'
        folder_id = FOLDER_IDS.get(pub_key, {}).get(folder_type)

        if not folder_id:
            return jsonify({'error': 'Folder ID not configured'}), 400

        # Verify folder access first (supports Shared Drives)
        try:
            folder_check = drive_service.files().get(
                fileId=folder_id,
                fields='id, name',
                supportsAllDrives=True
            ).execute()
            print(f"[API] Folder access OK: {folder_check.get('name')}")
        except Exception as folder_err:
            return jsonify({
                'error': f"Cannot access Google Drive folder. Ensure the service account has access. Folder ID: {folder_id}. Error: {str(folder_err)}"
            }), 500

        # Create the document (supports Shared Drives)
        doc_metadata = {
            'name': title,
            'mimeType': 'application/vnd.google-apps.document',
            'parents': [folder_id]
        }

        doc = drive_service.files().create(
            body=doc_metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()
        doc_id = doc.get('id')

        # Parse HTML for formatting if available
        text_content = article
        formatting_requests = []

        if article_html:
            parsed_text, parser = parse_html_for_docs(article_html)
            if parsed_text and parser:
                text_content = parsed_text
                formatting_requests = parser.get_docs_requests(start_index=1)

        # Add content to document
        requests_list = [
            {
                'insertText': {
                    'location': {'index': 1},
                    'text': text_content
                }
            }
        ]

        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': requests_list}
        ).execute()

        # Apply formatting if available
        if formatting_requests:
            try:
                docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={'requests': formatting_requests}
                ).execute()
                print(f"[API] Applied {len(formatting_requests)} formatting requests")
            except Exception as fmt_err:
                print(f"[API] Formatting error (continuing): {fmt_err}")

        # Make document accessible (supports Shared Drives)
        drive_service.permissions().create(
            fileId=doc_id,
            body={'type': 'anyone', 'role': 'reader'},
            supportsAllDrives=True
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

        return jsonify({
            'success': True,
            'doc_id': doc_id,
            'doc_url': doc_url,
            'title': title,
            'folder': folder_type,
            'formatting_applied': len(formatting_requests) > 0
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================
# Routes - Email Notifications
# ===================

@app.route('/api/send-notification', methods=['POST'])
def send_notification():
    """Send email notification"""
    data = request.json
    notification_type = data.get('type', 'draft')  # 'draft' or 'final'
    doc_url = data.get('doc_url')
    publication = data.get('publication')
    month = data.get('month')
    year = data.get('year', datetime.now().year)
    title = data.get('title', 'CEO Article')
    custom_recipients = data.get('recipients')  # Custom recipients from frontend

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))

        # Use custom recipients if provided, otherwise fall back to defaults
        if custom_recipients and len(custom_recipients) > 0:
            recipients = custom_recipients
        else:
            recipients = FINAL_RECIPIENTS if notification_type == 'final' else DRAFT_RECIPIENTS

        # Get proper publication display name (capitalized)
        pub_display_name = get_pub_display_name(publication)

        # Build email content
        if notification_type == 'final':
            subject = f"{pub_display_name} {month} (Final) - Ready for Submission"
            status_text = "FINAL - Ready for Submission"
            status_color = "#28a745"
        else:
            subject = f"{pub_display_name} {month} (Draft) - Ready for Review"
            status_text = "DRAFT - Ready for Review"
            status_color = "#FE8916"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #008181; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 8px 8px; }}
                .status {{ display: inline-block; background: {status_color}; color: white; padding: 5px 15px; border-radius: 20px; font-weight: bold; }}
                .button {{ display: inline-block; background: #008181; color: white; padding: 12px 30px; text-decoration: none; border-radius: 8px; margin-top: 20px; }}
                .details {{ background: white; padding: 15px; border-radius: 8px; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">CEO Article Generator</h1>
                    <p style="margin: 10px 0 0 0;">BriteCo Thought Leadership</p>
                </div>
                <div class="content">
                    <p class="status">{status_text}</p>

                    <div class="details">
                        <p><strong>Publication:</strong> {pub_display_name}</p>
                        <p><strong>Month:</strong> {month} {year}</p>
                        <p><strong>Title:</strong> {title}</p>
                    </div>

                    <p>{"This article is finalized and ready for submission to " + pub_display_name + "." if notification_type == 'final' else "Please review this draft and make any necessary edits."}</p>

                    <a href="{doc_url}" class="button" style="color: white;">Open in Google Docs</a>

                    <p style="margin-top: 30px; font-size: 12px; color: #666;">
                        This email was sent by the CEO Article Generator tool.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        # Send to each recipient
        sent_count = 0
        errors = []

        for recipient in recipients:
            try:
                message = Mail(
                    from_email=(
                        os.environ.get('SENDGRID_FROM_EMAIL', 'marketing@brite.co'),
                        os.environ.get('SENDGRID_FROM_NAME', 'BriteCo CEO Articles')
                    ),
                    to_emails=recipient.strip(),
                    subject=subject,
                    html_content=html_content
                )
                response = sg.send(message)
                if response.status_code in [200, 201, 202]:
                    sent_count += 1
                else:
                    errors.append(f"Failed for {recipient}: status {response.status_code}")
            except Exception as e:
                errors.append(f"Failed for {recipient}: {str(e)}")

        return jsonify({
            'success': True,
            'sent_count': sent_count,
            'total_recipients': len(recipients),
            'errors': errors if errors else None
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================
# ClickUp Integration
# ===================

def clickup_request(method, path, json_data=None):
    """Make an authenticated request to ClickUp API v2.
    Returns (success: bool, data: dict). Never raises."""
    if not CLICKUP_API_TOKEN:
        print("[CLICKUP] Skipped: CLICKUP_API_TOKEN not configured")
        return False, {'error': 'ClickUp not configured'}

    try:
        url = f"https://api.clickup.com/api/v2{path}"
        headers = {
            'Authorization': CLICKUP_API_TOKEN,
            'Content-Type': 'application/json'
        }
        resp = http_requests.request(method, url, headers=headers, json=json_data, timeout=10)

        if resp.status_code in (200, 201):
            return True, resp.json()
        else:
            print(f"[CLICKUP] API error {resp.status_code}: {resp.text[:300]}")
            return False, {'error': f"ClickUp API returned {resp.status_code}"}
    except Exception as e:
        print(f"[CLICKUP] Request failed: {e}")
        return False, {'error': str(e)}


def create_clickup_task(headline, publication, doc_url=None):
    """Create a ClickUp task for a new article. Returns task_id or None."""
    if not CLICKUP_LIST_ID:
        return None

    description = f"Publication: {get_pub_display_name(publication)}\nCreated by CEO Article Generator"
    if doc_url:
        description += f"\n\nDraft: {doc_url}"

    task_data = {
        'name': f"[{get_pub_display_name(publication)}] {headline}",
        'status': 'being written',
        'description': description
    }

    success, data = clickup_request('POST', f'/list/{CLICKUP_LIST_ID}/task', task_data)
    if success:
        task_id = data.get('id')
        print(f"[CLICKUP] Created task: {task_id} - {headline}")
        return task_id
    return None


def update_clickup_task_status(task_id, status, doc_url=None):
    """Update a ClickUp task's status. Optionally append a doc link to description."""
    if not task_id:
        print("[CLICKUP] Skipped status update: no task_id")
        return False

    update_data = {'status': status}

    # If a doc_url is provided, fetch current description and append the link
    if doc_url:
        ok, task_data = clickup_request('GET', f'/task/{task_id}')
        if ok:
            current_desc = task_data.get('description', '') or ''
            update_data['description'] = current_desc + f"\n\nFinal: {doc_url}"

    success, _ = clickup_request('PUT', f'/task/{task_id}', update_data)
    if success:
        print(f"[CLICKUP] Updated task {task_id} -> '{status}'")
    return success


@app.route('/api/clickup/create-task', methods=['POST'])
def clickup_create_task():
    """Create a ClickUp task for a new article"""
    data = request.json
    headline = data.get('headline', 'Untitled Article')
    publication = data.get('publication', '')
    doc_url = data.get('doc_url')

    task_id = create_clickup_task(headline, publication, doc_url)

    return jsonify({
        'success': task_id is not None,
        'clickup_task_id': task_id
    })


@app.route('/api/clickup/update-status', methods=['POST'])
def clickup_update_status():
    """Update a ClickUp task's status"""
    data = request.json
    task_id = data.get('clickup_task_id')
    status = data.get('status')
    doc_url = data.get('doc_url')

    if not task_id:
        return jsonify({'success': True, 'skipped': True, 'reason': 'No task_id'})

    if not status:
        return jsonify({'success': False, 'error': 'status required'}), 400

    success = update_clickup_task_status(task_id, status, doc_url)

    return jsonify({'success': success})


@app.route('/api/clickup/setup-webhook', methods=['GET'])
def setup_clickup_webhook():
    """One-time setup: register ClickUp webhook for task status changes"""
    if not CLICKUP_API_TOKEN:
        return jsonify({'success': False, 'error': 'CLICKUP_API_TOKEN not set'}), 400

    # Get workspace/team ID
    ok, teams_data = clickup_request('GET', '/team')
    if not ok or not teams_data.get('teams'):
        return jsonify({'success': False, 'error': 'Could not fetch ClickUp teams', 'detail': teams_data}), 500

    team_id = teams_data['teams'][0]['id']

    # Register webhook pointing back to this app
    webhook_url = request.host_url.rstrip('/') + '/api/clickup/webhook'
    ok, data = clickup_request('POST', f'/team/{team_id}/webhook', {
        'endpoint': webhook_url,
        'events': ['taskStatusUpdated']
    })

    if ok:
        return jsonify({'success': True, 'webhook_id': data.get('webhook', {}).get('id'), 'endpoint': webhook_url})
    else:
        return jsonify({'success': False, 'error': data}), 500


# ===================
# Todoist + Published Calendar Helpers
# ===================

def find_article_by_clickup_task_id(task_id):
    """Search completed/ then drafts/ in GCS for article with matching clickup_task_id"""
    if not gcs_client or not task_id:
        return None
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    for prefix in ['completed/', 'drafts/']:
        for blob in bucket.list_blobs(prefix=prefix):
            try:
                article = json.loads(blob.download_as_text())
                if article.get('clickup_task_id') == task_id:
                    return article
            except Exception:
                continue
    return None


def create_todoist_task(content):
    """Create a task in Todoist"""
    if not TODOIST_API_TOKEN:
        print("[TODOIST] Skipped: no API token configured")
        return
    payload = {'content': content}
    if TODOIST_PROJECT_ID:
        payload['project_id'] = TODOIST_PROJECT_ID
    resp = requests.post(
        'https://api.todoist.com/api/v1/tasks',
        headers={'Authorization': f'Bearer {TODOIST_API_TOKEN}', 'Content-Type': 'application/json'},
        json=payload
    )
    if resp.ok:
        print(f"[TODOIST] Created task: {content}")
    else:
        print(f"[TODOIST] Error {resp.status_code}: {resp.text[:200]}")


@app.route('/api/todoist/test', methods=['GET'])
def todoist_test():
    """Send a test task to Todoist to verify the integration works"""
    token_live = os.environ.get('_TODOIST_API_TOKEN', '')
    if not token_live and not TODOIST_API_TOKEN:
        return jsonify({
            'success': False,
            'error': 'TODOIST_API_TOKEN not set',
            'debug': {
                'module_var_set': bool(TODOIST_API_TOKEN),
                'env_var_set': bool(token_live),
                'env_keys_with_todoist': [k for k in os.environ.keys() if 'todoist' in k.lower()]
            }
        })
    payload = {'content': 'TEST - Todoist integration working!'}
    if TODOIST_PROJECT_ID:
        payload['project_id'] = TODOIST_PROJECT_ID
    resp = requests.post(
        'https://api.todoist.com/api/v1/tasks',
        headers={'Authorization': f'Bearer {TODOIST_API_TOKEN}', 'Content-Type': 'application/json'},
        json=payload
    )
    if resp.ok:
        return jsonify({'success': True, 'task': resp.json()})
    else:
        return jsonify({'success': False, 'status': resp.status_code, 'error': resp.text[:500]})


def get_clickup_task_info(task_id):
    """Fetch task details from ClickUp API (title, publication custom field)"""
    ok, data = clickup_request('GET', f'/task/{task_id}')
    if not ok:
        return None, None

    title = data.get('name', 'Untitled')
    publication = None

    # Check custom fields for "Publication"
    for field in data.get('custom_fields', []):
        if field.get('name', '').lower() == 'publication':
            # Dropdown/label type field
            type_config = field.get('type_config', {})
            options = {opt['orderindex']: opt['name'] for opt in type_config.get('options', [])}
            value = field.get('value')
            if isinstance(value, int):
                publication = options.get(value)
            elif isinstance(value, str):
                publication = value
            break

    # Fallback: parse pub name from title prefix like "[Forbes] headline"
    if not publication and title.startswith('['):
        bracket_end = title.find(']')
        if bracket_end > 0:
            publication = title[1:bracket_end]

    print(f"[CLICKUP] Task info: title={title}, publication={publication}")
    return title, publication


def append_published_entry(entry):
    """Append entry to published/entries.json in GCS"""
    if not gcs_client:
        return
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob('published/entries.json')
    entries = []
    if blob.exists():
        try:
            entries = json.loads(blob.download_as_text())
        except Exception:
            entries = []
    entries.append(entry)
    blob.upload_from_string(json.dumps(entries, indent=2), content_type='application/json')
    print(f"[PUBLISHED] Added entry: {entry.get('title', 'Untitled')}")


# ===================
# ClickUp Webhook
# ===================

@app.route('/api/clickup/webhook', methods=['POST'])
def clickup_webhook():
    """Receive ClickUp task status change webhooks"""
    data = request.json or {}

    if data.get('event') != 'taskStatusUpdated':
        return jsonify({'ok': True})

    # Extract new status from history_items
    new_status = None
    for item in data.get('history_items', []):
        if item.get('field') == 'status':
            new_status = (item.get('after', {}).get('status') or '').lower()
            break

    if not new_status:
        return jsonify({'ok': True})

    task_id = data.get('task_id')
    print(f"[CLICKUP WEBHOOK] task_id={task_id} new_status={new_status}")

    # Try GCS first, then fall back to ClickUp API for task details
    article = find_article_by_clickup_task_id(task_id)

    if new_status == 'submited':
        if article:
            pub_name = get_pub_display_name(article.get('publication', ''))
            title = article.get('data', {}).get('topic', {}).get('headline', '')
        else:
            title, pub = get_clickup_task_info(task_id)
            pub_name = pub or 'Article'
            title = title or ''
            # Strip [Pub Name] prefix from title if present
            if title.startswith('[') and ']' in title:
                title = title[title.index(']') + 1:].strip()
        create_todoist_task(f"{pub_name} article submitted ({title}) - time to record")

    elif new_status == 'published':
        if article:
            title = article.get('data', {}).get('topic', {}).get('headline', 'Untitled')
            publication = article.get('publication')
            doc_url = article.get('data', {}).get('doc_url')
        else:
            title, publication = get_clickup_task_info(task_id)
            title = title or 'Untitled'
            doc_url = None

        entry = {
            'draft_id': article.get('id') if article else None,
            'title': title,
            'publication': publication,
            'published_at': datetime.now().isoformat(),
            'doc_url': doc_url
        }
        append_published_entry(entry)

    return jsonify({'ok': True})


# ===================
# Published Calendar
# ===================

@app.route('/api/published/list', methods=['GET'])
def list_published():
    """List published articles from GCS"""
    if not gcs_client:
        return jsonify({'published': []})
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob('published/entries.json')
    if not blob.exists():
        return jsonify({'published': []})
    try:
        entries = json.loads(blob.download_as_text())
        entries.sort(key=lambda x: x.get('published_at', ''), reverse=True)
        return jsonify({'published': entries})
    except Exception as e:
        return jsonify({'published': [], 'error': str(e)})


# ===================
# Health Check
# ===================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0'
    })

# ===================
# Run Application
# ===================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
