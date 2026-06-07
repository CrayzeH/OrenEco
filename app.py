from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from flask_cors import CORS
import sqlite3
import json
import requests
import time
import uuid
import re
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps
from external_environment_data import (
    ensure_environment_columns,
    sync_forests_from_overpass,
    sync_pollution_from_openmeteo,
    sync_pollution_from_openaq,
    sync_region_boundary_from_overpass,
)

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
CORS(app)

DATABASE = 'oreneco.db'

# Конфигурация GigaChat
CLIENT_ID = "019d5ea9-24f7-746e-a329-eb8884ed04ab"
AUTHORIZATION_KEY = "MDE5ZDVlYTktMjRmNy03NDZlLWEzMjktZWI4ODg0ZWQwNGFiOmUyMTM4YWMzLTRkYzItNDEwYy1hOTAyLTk0MTI0NTBhZWY0Yg=="
GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

token_cache = {'token': None, 'expires_at': 0}


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_contact_messages_table(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.commit()


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def check_password(password, password_hash):
    return hash_password(password) == password_hash


# Декораторы для проверки ролей
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        if session.get('role') != 'admin':
            return jsonify({'error': 'Доступ запрещен. Требуются права администратора'}), 403
        return f(*args, **kwargs)

    return decorated_function


def expert_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        if session.get('role') not in ['expert', 'admin']:
            return jsonify({'error': 'Доступ запрещен. Требуются права эксперта'}), 403
        return f(*args, **kwargs)

    return decorated_function


# Функции для GigaChat
def get_access_token():
    global token_cache
    if token_cache['token'] and token_cache['expires_at'] > time.time():
        return token_cache['token']
    try:
        rquid = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rquid,
            "Authorization": f"Basic {AUTHORIZATION_KEY}"
        }
        data = {"scope": "GIGACHAT_API_PERS"}
        import urllib3
        urllib3.disable_warnings()
        response = requests.post(GIGACHAT_AUTH_URL, headers=headers, data=data, verify=False, timeout=30)
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')
            expires_in = token_data.get('expires_in', 1800)
            if access_token:
                token_cache['token'] = access_token
                token_cache['expires_at'] = time.time() + expires_in - 60
                return access_token
        return None
    except Exception as e:
        print(f"Ошибка получения токена: {str(e)}")
        return None


def extract_score_from_response(text):
    patterns = [r'Оценка:\s*(\d{1,2})', r'оценка[:\s]*(\d{1,2})', r'(\d{1,2})\s*из\s*10', r'(\d{1,2})\/10']
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            score = int(match.group(1))
            if 0 <= score <= 10:
                return score
    return 5


def ask_gigachat(prompt):
    token = get_access_token()
    if not token:
        return "🤖 ИИ-ассистент временно недоступен", 5
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [
                {"role": "system",
                 "content": "Ты опытный эколог. В конце ответа ОБЯЗАТЕЛЬНО укажи оценку в формате: Оценка: X/10"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 800
        }
        import urllib3
        urllib3.disable_warnings()
        response = requests.post(GIGACHAT_API_URL, headers=headers, json=payload, verify=False, timeout=60)
        if response.status_code == 200:
            result = response.json()
            message = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            score = extract_score_from_response(message)
            return message.strip(), score
        return f"Ошибка {response.status_code}", 5
    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        return f"Ошибка: {str(e)}", 5


# ==================== СТРАНИЦЫ ====================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/map')
def map_page():
    return render_template('map.html')


@app.route('/contacts')
def contacts_page():
    return render_template('contacts.html')


@app.route('/api/contact', methods=['POST'])
def create_contact_message():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    message = (data.get('message') or '').strip()

    if not name or not email or not message:
        return jsonify({'error': 'Заполните все обязательные поля'}), 400

    if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
        return jsonify({'error': 'Введите корректный email'}), 400

    db = get_db()
    ensure_contact_messages_table(db)
    db.execute('''
        INSERT INTO contact_messages (name, email, message)
        VALUES (?, ?, ?)
    ''', (name, email, message))
    db.commit()
    db.close()

    return jsonify({'success': True})


@app.route('/expertise')
def expertise():
    return render_template('expertise.html')


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/register')
def register_page():
    return render_template('register.html')


@app.route('/profile')
@login_required
def profile_page():
    return render_template('profile.html')


@app.route('/admin')
@admin_required
def admin_page():
    return render_template('admin.html')


@app.route('/articles')
def articles_page():
    return render_template('articles.html')


@app.route('/charts')
def charts_page():
    return render_template('charts.html')


@app.route('/article/<int:article_id>')
def article_detail_page(article_id):
    return render_template('article_detail.html', article_id=article_id)


@app.route('/create-article')
@expert_required
def create_article_page():
    return render_template('create_article.html')


# ==================== API АВТОРИЗАЦИИ ====================
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE username = ? AND is_active = 1', (username,)).fetchone()
    db.close()

    if user and check_password(password, user['password_hash']):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session['full_name'] = user['full_name']

        db = get_db()
        db.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
        db.commit()
        db.close()

        return jsonify({'success': True, 'role': user['role']})
    return jsonify({'success': False, 'error': 'Неверное имя пользователя или пароль'})


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    full_name = data.get('full_name')

    if not all([username, email, password]):
        return jsonify({'success': False, 'error': 'Заполните все поля'})
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Пароль должен быть не менее 6 символов'})

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE username = ? OR email = ?', (username, email)).fetchone()
    if existing:
        db.close()
        return jsonify({'success': False, 'error': 'Пользователь уже существует'})

    password_hash = hash_password(password)
    db.execute(
        'INSERT INTO users (username, email, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)',
        (username, email, password_hash, full_name, 'user'))
    db.commit()

    user = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    db.close()

    session['user_id'] = user['id']
    session['username'] = username
    session['role'] = 'user'

    return jsonify({'success': True, 'redirect': '/profile'})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/current-user')
def api_current_user():
    if 'user_id' in session:
        return jsonify({
            'authenticated': True,
            'user_id': session['user_id'],
            'username': session.get('username'),
            'role': session.get('role'),
            'full_name': session.get('full_name')
        })
    return jsonify({'authenticated': False})


# ==================== API УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ (АДМИН) ====================
@app.route('/api/users')
@admin_required
def get_users():
    db = get_db()
    users = db.execute(
        'SELECT id, username, email, full_name, role, is_active, created_at, last_login FROM users').fetchall()
    db.close()
    return jsonify([dict(u) for u in users])


@app.route('/api/users/<int:user_id>/role', methods=['PUT'])
@admin_required
def update_user_role(user_id):
    data = request.get_json()
    new_role = data.get('role')

    if new_role not in ['user', 'expert', 'admin']:
        return jsonify({'error': 'Недопустимая роль'}), 400

    if user_id == session['user_id'] and new_role != session['role']:
        return jsonify({'error': 'Нельзя изменить свою роль'}), 400

    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/users/<int:user_id>/toggle', methods=['PUT'])
@admin_required
def toggle_user_active(user_id):
    if user_id == session['user_id']:
        return jsonify({'error': 'Нельзя заблокировать самого себя'}), 400

    db = get_db()
    user = db.execute('SELECT is_active FROM users WHERE id = ?', (user_id,)).fetchone()
    if user:
        new_status = 0 if user['is_active'] else 1
        db.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, user_id))
        db.commit()
    db.close()
    return jsonify({'success': True})


# ==================== API КАРТ (ЗАГРЯЗНЕНИЕ И ЛЕСА) ====================
@app.route('/api/pollution')
def get_pollution():
    db = get_db()
    ensure_environment_columns(db)
    points = db.execute('''
        SELECT id, name, lat, lon, pollution, pollutants, description, source, source_id, measured_at
        FROM pollution_points
        WHERE is_active = 1
    ''').fetchall()
    db.close()
    result = []
    for point in points:
        p = dict(point)
        p['pollutants'] = json.loads(p['pollutants']) if p['pollutants'] else []
        result.append(p)
    return jsonify(result)


@app.route('/api/forests')
def get_forests():
    db = get_db()
    ensure_environment_columns(db)
    forests = db.execute('''
        SELECT id, name, coordinates, status, description, area, source, source_id, category
        FROM forest_areas
        WHERE is_active = 1
    ''').fetchall()
    db.close()
    result = []
    for forest in forests:
        f = dict(forest)
        f['coordinates'] = json.loads(f['coordinates']) if f['coordinates'] else []
        result.append(f)
    return jsonify(result)


@app.route('/api/admin/sync/forests', methods=['POST'])
@admin_required
def sync_forests():
    try:
        db = get_db()
        result = sync_forests_from_overpass(db)
        db.close()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/sync/pollution', methods=['POST'])
@admin_required
def sync_pollution():
    try:
        db = get_db()
        result = sync_pollution_from_openmeteo(db, ai_explainer=lambda prompt: ask_gigachat(prompt)[0])
        db.close()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/region-boundary')
def get_region_boundary():
    db = get_db()
    ensure_environment_columns(db)
    boundary = db.execute('''
        SELECT id, name, coordinates, source, source_id
        FROM region_boundaries
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT 1
    ''').fetchone()
    db.close()

    if not boundary:
        return jsonify({'name': 'Оренбургская область', 'coordinates': [], 'source': None})

    result = dict(boundary)
    result['coordinates'] = json.loads(result['coordinates']) if result['coordinates'] else []
    return jsonify(result)


@app.route('/api/admin/sync/boundary', methods=['POST'])
@admin_required
def sync_boundary():
    try:
        db = get_db()
        result = sync_region_boundary_from_overpass(db)
        db.close()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/charts-data')
def get_charts_data():
    db = get_db()

    pollution_levels = db.execute('''
        SELECT
            CASE
                WHEN pollution <= 20 THEN 'Хорошее качество'
                WHEN pollution <= 40 THEN 'Удовлетворительное'
                WHEN pollution <= 60 THEN 'Умеренное'
                WHEN pollution <= 80 THEN 'Плохое'
                ELSE 'Очень плохое'
            END as label,
            COUNT(*) as count
        FROM pollution_points
        WHERE is_active = 1
        GROUP BY label
    ''').fetchall()

    project_types = db.execute('''
        SELECT COALESCE(type, 'Не указан') as label, COUNT(*) as count
        FROM projects
        WHERE is_active = 1
        GROUP BY type
        ORDER BY count DESC
    ''').fetchall()

    project_decisions = db.execute('''
        SELECT COALESCE(real_decision, 'pending') as label, COUNT(*) as count
        FROM projects
        WHERE is_active = 1
        GROUP BY real_decision
    ''').fetchall()

    article_categories = db.execute('''
        SELECT COALESCE(category, 'Без категории') as label, COUNT(*) as count
        FROM articles
        GROUP BY category
        ORDER BY count DESC
    ''').fetchall()

    expertise_scores = db.execute('''
        SELECT
            CASE
                WHEN ai_score >= 8 THEN '8-10 баллов'
                WHEN ai_score >= 5 THEN '5-7 баллов'
                ELSE '0-4 балла'
            END as label,
            COUNT(*) as count
        FROM user_expertise_results
        GROUP BY label
    ''').fetchall()

    summary = db.execute('''
        SELECT
            (SELECT COUNT(*) FROM pollution_points WHERE is_active = 1) as total_points,
            (SELECT ROUND(AVG(pollution), 1) FROM pollution_points WHERE is_active = 1) as avg_pollution,
            (SELECT COUNT(*) FROM projects WHERE is_active = 1) as total_projects,
            (SELECT COUNT(*) FROM articles) as total_articles,
            (SELECT ROUND(AVG(ai_score), 1) FROM user_expertise_results) as avg_score
    ''').fetchone()

    db.close()

    decision_map = {
        'approve': 'Согласован',
        'revise': 'На доработке',
        'reject': 'Отклонен',
        'pending': 'Ожидает'
    }

    def make_dataset(rows, mapper=None):
        labels = []
        values = []
        for row in rows:
            label = row['label']
            labels.append(mapper.get(label, label) if mapper else label)
            values.append(row['count'])
        return {'labels': labels, 'values': values}

    return jsonify({
        'summary': {
            'total_points': summary['total_points'] or 0,
            'avg_pollution': summary['avg_pollution'] or 0,
            'total_projects': summary['total_projects'] or 0,
            'total_articles': summary['total_articles'] or 0,
            'avg_score': summary['avg_score'] or 0
        },
        'datasets': {
            'pollution_levels': make_dataset(pollution_levels),
            'project_types': make_dataset(project_types),
            'project_decisions': make_dataset(project_decisions, decision_map),
            'article_categories': make_dataset(article_categories),
            'expertise_scores': make_dataset(expertise_scores)
        }
    })


# ==================== API ПРОЕКТОВ ====================
@app.route('/api/projects-list')
def get_projects_list():
    db = get_db()
    projects = db.execute('''
        SELECT id, title, type, region, investment_amount, points, real_decision
        FROM projects WHERE is_active = 1 ORDER BY points DESC
    ''').fetchall()

    user_results = {}
    if 'user_id' in session:
        results = db.execute('''
            SELECT project_id, MAX(ai_score) as best_score, COUNT(*) as attempts
            FROM user_expertise_results 
            WHERE user_id = ?
            GROUP BY project_id
        ''', (session['user_id'],)).fetchall()

        for r in results:
            user_results[r['project_id']] = {
                'best_score': r['best_score'],
                'attempts': r['attempts']
            }

    db.close()

    decision_map = {'approve': 'Согласован', 'revise': 'На доработке', 'reject': 'Отклонен', 'pending': 'Ожидает'}

    result = []
    for p in projects:
        project_data = {
            'id': p['id'],
            'title': p['title'],
            'type': p['type'],
            'region': p['region'] or 'Регион не указан',
            'investment': p['investment_amount'] or 0,
            'points': p['points'] or 0,
            'real_decision': decision_map.get(p['real_decision'], 'Ожидает')
        }

        if p['id'] in user_results:
            project_data['user_best_score'] = user_results[p['id']]['best_score']
            project_data['user_attempts'] = user_results[p['id']]['attempts']
            project_data['is_completed'] = True
        else:
            project_data['user_best_score'] = None
            project_data['user_attempts'] = 0
            project_data['is_completed'] = False

        result.append(project_data)

    return jsonify(result)


@app.route('/api/projects/<int:project_id>')
def get_project_details(project_id):
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ? AND is_active = 1', (project_id,)).fetchone()
    db.close()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    return jsonify(dict(project))


@app.route('/api/projects', methods=['POST'])
@admin_required
def create_project():
    data = request.get_json()
    required = ['title', 'type', 'region']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Поле {field} обязательно'}), 400

    db = get_db()
    cursor = db.execute('''
        INSERT INTO projects (title, type, initiator, region, district, total_area, disturbed_area,
        distance_to_settlements, landscape_type, soil_type, distance_to_water, water_body_name,
        groundwater_depth, red_book_species, vegetation_type, technology_type, waste_volume,
        water_consumption, compensation_measures, points, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    ''', (
        data.get('title'), data.get('type'), data.get('initiator'), data.get('region'), data.get('district'),
        data.get('total_area'), data.get('disturbed_area'), data.get('distance_to_settlements'),
        data.get('landscape_type'), data.get('soil_type'), data.get('distance_to_water'), data.get('water_body_name'),
        data.get('groundwater_depth'), data.get('red_book_species'), data.get('vegetation_type'),
        data.get('technology_type'), data.get('waste_volume'), data.get('water_consumption'),
        data.get('compensation_measures'), data.get('points', 5)
    ))
    db.commit()
    project_id = cursor.lastrowid
    db.close()
    return jsonify({'success': True, 'id': project_id})


# ==================== API ЭКСПЕРТИЗЫ ====================
@app.route('/api/ai-expertise', methods=['POST'])
@login_required
def ai_expertise():
    """Жесткая ИИ-экспертиза с детальным анализом"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        expert_opinion = data.get('expert_opinion', '').strip()
        decision = data.get('decision')

        # Валидация длины
        if len(expert_opinion) < 100:
            return jsonify({'error': '❌ Экспертное заключение слишком короткое (минимум 100 символов). Напишите развернутый анализ!'}), 400

        # Получаем проект
        db = get_db()
        project = db.execute('SELECT * FROM projects WHERE id = ? AND is_active = 1', (project_id,)).fetchone()
        db.close()

        if not project:
            return jsonify({'error': 'Проект не найден'}), 404

        # Расширенный промпт для ИИ
        decision_rus = {
            'approve': 'СОГЛАСОВАТЬ',
            'revise': 'ОТПРАВИТЬ НА ДОРАБОТКУ',
            'reject': 'ОТКЛОНИТЬ'
        }

        prompt = f"""Ты — строгий председатель экспертной комиссии Государственной экологической экспертизы.

Твоя задача — детально проанализировать экспертное заключение пользователя по проекту.

## ДАННЫЕ ПРОЕКТА:
- Название: {project['title']}
- Тип: {project['type']}
- Инициатор: {project['initiator'] or 'Не указан'}
- Регион: {project['region'] or 'Не указан'}
- Инвестиции: {project['investment_amount'] or '0'} млн руб.

## ПРИРОДНЫЕ УСЛОВИЯ:
- Ландшафт: {project['landscape_type'] or 'Не указан'}
- Почвы: {project['soil_type'] or 'Не указан'}
- Расстояние до водного объекта: {project['distance_to_water'] or '0'} км
- Водный объект: {project['water_body_name'] or 'Не указан'}
- Глубина грунтовых вод: {project['groundwater_depth'] or '0'} м

## БИОРАЗНООБРАЗИЕ:
- Краснокнижные виды: {project['red_book_species'] or 'отсутствуют'}
- Тип растительности: {project['vegetation_type'] or 'Не указан'}

## ТЕХНОЛОГИЧЕСКИЕ ПАРАМЕТРЫ:
- Технология: {project['technology_type'] or 'Не указана'}
- Объем отходов: {project['waste_volume'] or '0'} тыс. тонн/год
- Водопотребление: {project['water_consumption'] or '0'} тыс. м³/год

## КОМПЕНСАЦИОННЫЕ МЕРЫ:
{project['compensation_measures'] or 'Не указаны'}

## РЕШЕНИЕ ЭКСПЕРТА (пользователя): {decision_rus[decision]}

## ТЕКСТ ЭКСПЕРТНОГО ЗАКЛЮЧЕНИЯ ПОЛЬЗОВАТЕЛЯ:
{expert_opinion}

---

## ТВОЯ ЗАДАЧА — ДЕТАЛЬНО ПРОАНАЛИЗИРОВАТЬ заключение по следующим пунктам:

### 1. АНАЛИЗ ПОЛНОТЫ (оцени от 0 до 3 баллов)
- Учтены ли все ключевые аспекты проекта?
- Проанализировано ли воздействие на водные ресурсы?
- Учтены ли краснокнижные виды?
- Есть ли оценка воздействия на почвы и растительность?
- Проанализированы ли технологические риски?

### 2. ОБОСНОВАННОСТЬ ВЫВОДОВ (оцени от 0 до 3 баллов)
- Подкреплены ли выводы конкретными фактами из проекта?
- Есть ли ссылки на расстояния, объемы, проценты?
- Приведены ли количественные оценки рисков?
- Логична ли аргументация?

### 3. УЧЕТ ЭКОЛОГИЧЕСКИХ РИСКОВ (оцени от 0 до 2 баллов)
- Определены ли основные экологические риски?
- Предложены ли конкретные меры по их снижению?
- Учтена ли специфика региона?

### 4. ПРОФЕССИОНАЛИЗМ ЯЗЫКА (оцени от 0 до 2 баллов)
- Используется ли профессиональная терминология?
- Есть ли ссылки на нормативные документы?
- Грамотно ли сформулированы выводы?

### ШТРАФНЫЕ БАЛЛЫ:
- Если заключение короче 300 символов: -1 балл
- Если нет ни одного конкретного числа из проекта: -1 балл
- Если краснокнижные виды упомянуты, но не проанализированы: -1 балл
- Если заключение противоречит само себе: -2 балла

## ФОРМАТ ОТВЕТА (строго соблюдай структуру):

═══ РАЗБОР ЭКСПЕРТНОГО ЗАКЛЮЧЕНИЯ ═══

📌 ПОЛНОТА АНАЛИЗА (X/3)
[Что учтено, что упущено]

📌 ОБОСНОВАННОСТЬ (X/3)
[Есть ли цифры, факты, логика]

📌 ЭКОЛОГИЧЕСКИЕ РИСКИ (X/2)
[Какие риски выявлены, какие пропущены]

📌 ПРОФЕССИОНАЛИЗМ (X/2)
[Оценка языка и стиля]

═══ ШТРАФЫ ═══
[Перечислить примененные штрафы]

═══ ИТОГОВАЯ ОЦЕНКА: X/10 ═══
[1 предложение с главным выводом]

Будь максимально объективным и строгим. За плохой анализ ставь низкие оценки (0-3). За хороший — высокие (7-10)."""

        ai_response, score = ask_gigachat(prompt)
        score = max(0, min(10, score))

        # Сохраняем результат
        db2 = get_db()
        try:
            db2.execute('''INSERT INTO user_expertise_results 
                          (user_id, project_id, user_decision, user_comment, ai_score, ai_feedback)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (session['user_id'], project_id, decision, expert_opinion, score, ai_response[:2000]))
            db2.commit()
        except Exception as e:
            print(f"Ошибка сохранения: {e}")
        finally:
            db2.close()

        return jsonify({
            "analysis": ai_response,
            "score": score,
            "decision": decision
        })

    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==================== API СТАТИСТИКИ ПРОФИЛЯ ====================
@app.route('/api/user-stats')
@login_required
def user_stats():
    db = get_db()
    results = db.execute('''
        SELECT uer.*, p.title 
        FROM user_expertise_results uer
        JOIN projects p ON uer.project_id = p.id
        WHERE uer.user_id = ?
        ORDER BY uer.created_at DESC
    ''', (session['user_id'],)).fetchall()

    best_by_project = {}
    for r in results:
        if r['project_id'] not in best_by_project:
            best_by_project[r['project_id']] = r
        elif r['ai_score'] > best_by_project[r['project_id']]['ai_score']:
            best_by_project[r['project_id']] = r

    best_results = list(best_by_project.values())
    total_unique = len(best_results)
    avg_score = round(sum(r['ai_score'] for r in best_results) / total_unique, 1) if total_unique > 0 else 0
    best_score = max((r['ai_score'] for r in best_results), default=0)

    decision_map = {'approve': 'Согласован', 'revise': 'На доработку', 'reject': 'Отклонён'}
    recent = [{
        'title': r['title'],
        'decision': decision_map.get(r['user_decision'], r['user_decision']),
        'score': r['ai_score'],
        'date': r['created_at']
    } for r in results[:10]]

    db.close()

    return jsonify({
        'total_expertises': total_unique,
        'avg_score': avg_score,
        'best_score': best_score,
        'recent': recent
    })


# ==================== API СТАТЕЙ ====================
@app.route('/api/articles')
def get_articles():
    category = request.args.get('category')
    search = request.args.get('search')

    db = get_db()
    query = '''
        SELECT a.*, u.username as author_name
        FROM articles a
        JOIN users u ON a.created_by = u.id
        WHERE a.status = 'published'
    '''
    params = []

    if category:
        query += ' AND a.category = ?'
        params.append(category)
    if search:
        query += ' AND (a.title LIKE ? OR a.summary LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])

    query += ' ORDER BY a.created_at DESC'
    articles = db.execute(query, params).fetchall()
    db.close()

    result = []
    for a in articles:
        a_dict = dict(a)
        a_dict['authors'] = json.loads(a_dict['authors']) if a_dict['authors'] else []
        a_dict['keywords'] = json.loads(a_dict['keywords']) if a_dict['keywords'] else []
        result.append(a_dict)
    return jsonify(result)


@app.route('/api/articles/<int:article_id>')
def get_article(article_id):
    db = get_db()
    db.execute('UPDATE articles SET views = views + 1 WHERE id = ?', (article_id,))
    db.commit()

    article = db.execute('''
        SELECT a.*, u.username as author_name, u.id as author_id
        FROM articles a
        JOIN users u ON a.created_by = u.id
        WHERE a.id = ? AND a.status = 'published'
    ''', (article_id,)).fetchone()

    comments = db.execute('''
        SELECT c.*, u.username
        FROM article_comments c
        JOIN users u ON c.user_id = u.id
        WHERE c.article_id = ?
        ORDER BY c.created_at DESC
    ''', (article_id,)).fetchall()
    db.close()

    if not article:
        return jsonify({'error': 'Статья не найдена'}), 404

    result = dict(article)
    result['authors'] = json.loads(result['authors']) if result['authors'] else []
    result['keywords'] = json.loads(result['keywords']) if result['keywords'] else []
    result['comments'] = [dict(c) for c in comments]
    return jsonify(result)


@app.route('/api/articles/<int:article_id>/like', methods=['POST'])
@login_required
def like_article(article_id):
    db = get_db()
    db.execute('UPDATE articles SET likes = likes + 1 WHERE id = ?', (article_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/articles/<int:article_id>/comment', methods=['POST'])
@login_required
def comment_article(article_id):
    data = request.get_json()
    comment = data.get('comment')
    if not comment:
        return jsonify({'error': 'Комментарий не может быть пустым'}), 400

    db = get_db()
    db.execute('INSERT INTO article_comments (article_id, user_id, comment) VALUES (?, ?, ?)',
               (article_id, session['user_id'], comment))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/generate-article', methods=['POST'])
@expert_required
def generate_article():
    data = request.get_json()
    topic = data.get('topic')
    category = data.get('category', 'экология')

    if not topic:
        return jsonify({'error': 'Укажите тему статьи'}), 400

    prompt = f"""Напиши научно-популярную статью на тему "{topic}" для экологического портала.
Категория: {category}

Требования:
1. Заголовок
2. Краткая аннотация (2-3 предложения)
3. Введение с актуальностью
4. Основная часть (2-3 раздела с подзаголовками)
5. Заключение и выводы
6. Список ключевых слов

Используй HTML-теги: <h2>, <h3>, <p>, <ul>, <li>.

В конце укажи: Оценка: X/10 (для проверки качества)"""

    ai_response, _ = ask_gigachat(prompt)

    # Извлекаем заголовок
    title_match = re.search(r'<h2>(.*?)</h2>', ai_response)
    title = title_match.group(1) if title_match else f"Статья: {topic}"

    # Извлекаем аннотацию
    summary_match = re.search(r'<p>(.*?)</p>', ai_response)
    summary = summary_match.group(1)[:200] if summary_match else ai_response[:200]

    return jsonify({
        'title': title,
        'content': ai_response,
        'summary': summary
    })


@app.route('/api/admin/projects', methods=['GET'])
@admin_required
def admin_get_all_projects():
    """Админ: получить все проекты (включая неактивные)"""
    db = get_db()
    projects = db.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    db.close()
    return jsonify([dict(p) for p in projects])


@app.route('/api/admin/projects', methods=['POST'])
@admin_required
def admin_create_project():
    """Админ: создать новый проект"""
    data = request.get_json()

    required = ['title', 'type', 'region']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Поле {field} обязательно'}), 400

    db = get_db()
    cursor = db.execute('''
        INSERT INTO projects (
            title, type, initiator, investment_amount, region, district,
            total_area, disturbed_area, distance_to_settlements,
            landscape_type, soil_type, protected_area_proximity,
            distance_to_water, water_body_name, groundwater_depth,
            red_book_species, vegetation_type, forest_cover_percent,
            construction_period, operation_period, technology_type,
            waste_volume, water_consumption, energy_consumption,
            compensation_measures, environmental_monitoring,
            real_decision, justification, points, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    ''', (
        data.get('title'), data.get('type'), data.get('initiator'), data.get('investment_amount'),
        data.get('region'), data.get('district'), data.get('total_area'), data.get('disturbed_area'),
        data.get('distance_to_settlements'), data.get('landscape_type'), data.get('soil_type'),
        data.get('protected_area_proximity'), data.get('distance_to_water'), data.get('water_body_name'),
        data.get('groundwater_depth'), data.get('red_book_species'), data.get('vegetation_type'),
        data.get('forest_cover_percent'), data.get('construction_period'), data.get('operation_period'),
        data.get('technology_type'), data.get('waste_volume'), data.get('water_consumption'),
        data.get('energy_consumption'), data.get('compensation_measures'), data.get('environmental_monitoring'),
        data.get('real_decision', 'pending'), data.get('justification'), data.get('points', 5)
    ))
    db.commit()
    project_id = cursor.lastrowid
    db.close()

    return jsonify({'success': True, 'id': project_id})


@app.route('/api/admin/projects/<int:project_id>', methods=['PUT'])
@admin_required
def admin_update_project(project_id):
    """Админ: обновить проект"""
    data = request.get_json()

    db = get_db()
    db.execute('''
        UPDATE projects SET
            title = ?, type = ?, initiator = ?, investment_amount = ?,
            region = ?, district = ?, total_area = ?, disturbed_area = ?,
            distance_to_water = ?, water_body_name = ?, groundwater_depth = ?,
            landscape_type = ?, red_book_species = ?, compensation_measures = ?,
            technology_type = ?, waste_volume = ?, points = ?
        WHERE id = ?
    ''', (
        data.get('title'), data.get('type'), data.get('initiator'), data.get('investment_amount'),
        data.get('region'), data.get('district'), data.get('total_area'), data.get('disturbed_area'),
        data.get('distance_to_water'), data.get('water_body_name'), data.get('groundwater_depth'),
        data.get('landscape_type'), data.get('red_book_species'), data.get('compensation_measures'),
        data.get('technology_type'), data.get('waste_volume'), data.get('points', 5),
        project_id
    ))
    db.commit()
    db.close()

    return jsonify({'success': True})


@app.route('/api/admin/projects/<int:project_id>', methods=['GET'])
@admin_required
def admin_get_project(project_id):
    """Админ: получить детали проекта"""
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
    db.close()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    return jsonify(dict(project))


@app.route('/api/admin/projects/<int:project_id>', methods=['DELETE'])
@admin_required
def admin_delete_project(project_id):
    """Админ: удалить проект (soft delete)"""
    db = get_db()
    db.execute('UPDATE projects SET is_active = 0 WHERE id = ?', (project_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ==================== API АДМИНА (УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ) ====================
@app.route('/api/admin/users')
@admin_required
def admin_get_users():
    """Админ: получить всех пользователей"""
    db = get_db()
    users = db.execute(
        'SELECT id, username, email, full_name, role, is_active, created_at, last_login FROM users').fetchall()
    db.close()
    return jsonify([dict(u) for u in users])


@app.route('/api/admin/users/<int:user_id>/role', methods=['PUT'])
@admin_required
def admin_update_user_role(user_id):
    """Админ: изменить роль пользователя"""
    data = request.get_json()
    new_role = data.get('role')

    if new_role not in ['user', 'expert', 'admin']:
        return jsonify({'error': 'Недопустимая роль'}), 400

    if user_id == session['user_id']:
        return jsonify({'error': 'Нельзя изменить свою роль'}), 400

    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/admin/users/<int:user_id>/toggle', methods=['PUT'])
@admin_required
def admin_toggle_user_active(user_id):
    """Админ: заблокировать/разблокировать пользователя"""
    if user_id == session['user_id']:
        return jsonify({'error': 'Нельзя заблокировать самого себя'}), 400

    db = get_db()
    user = db.execute('SELECT is_active FROM users WHERE id = ?', (user_id,)).fetchone()
    if user:
        new_status = 0 if user['is_active'] else 1
        db.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, user_id))
        db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/admin/contact-messages')
@admin_required
def admin_get_contact_messages():
    """Админ: получить обращения из формы контактов"""
    db = get_db()
    ensure_contact_messages_table(db)
    messages = db.execute('''
        SELECT id, name, email, message, status, created_at, updated_at
        FROM contact_messages
        ORDER BY created_at DESC
    ''').fetchall()
    db.close()
    return jsonify([dict(message) for message in messages])


@app.route('/api/admin/contact-messages/<int:message_id>/status', methods=['PUT'])
@admin_required
def admin_update_contact_message_status(message_id):
    """Админ: изменить статус обращения"""
    data = request.get_json() or {}
    status = data.get('status')

    if status not in ['new', 'read', 'closed']:
        return jsonify({'error': 'Некорректный статус обращения'}), 400

    db = get_db()
    ensure_contact_messages_table(db)
    cursor = db.execute('''
        UPDATE contact_messages
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (status, message_id))
    db.commit()
    db.close()

    if cursor.rowcount == 0:
        return jsonify({'error': 'Обращение не найдено'}), 404

    return jsonify({'success': True})


@app.route('/api/admin/stats')
@admin_required
def admin_get_stats():
    """Админ: получить общую статистику"""
    db = get_db()
    ensure_contact_messages_table(db)

    total_users = db.execute('SELECT COUNT(*) as count FROM users').fetchone()['count']
    total_projects = db.execute('SELECT COUNT(*) as count FROM projects WHERE is_active = 1').fetchone()['count']
    total_expertises = db.execute('SELECT COUNT(*) as count FROM user_expertise_results').fetchone()['count']
    total_messages = db.execute('SELECT COUNT(*) as count FROM contact_messages').fetchone()['count']
    new_messages = db.execute("SELECT COUNT(*) as count FROM contact_messages WHERE status = 'new'").fetchone()['count']
    avg_score = db.execute('SELECT AVG(ai_score) as avg FROM user_expertise_results').fetchone()['avg'] or 0

    db.close()

    return jsonify({
        'total_users': total_users,
        'total_projects': total_projects,
        'total_expertises': total_expertises,
        'total_messages': total_messages,
        'new_messages': new_messages,
        'avg_score': round(avg_score, 1)
    })


@app.route('/api/create-article', methods=['POST'])
@expert_required
def create_article():
    data = request.get_json()

    if not all([data.get('title'), data.get('content'), data.get('summary')]):
        return jsonify({'error': 'Заполните все обязательные поля'}), 400

    db = get_db()
    cursor = db.execute('''
        INSERT INTO articles (title, authors, category, content, summary, keywords, created_by, published_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'published')
    ''', (
        data['title'],
        json.dumps([data.get('author', session['username'])]),
        data.get('category', 'Экология'),
        data['content'],
        data['summary'],
        json.dumps(data.get('keywords', [])),
        session['user_id']
    ))
    db.commit()
    article_id = cursor.lastrowid
    db.close()

    return jsonify({'success': True, 'article_id': article_id})


if __name__ == '__main__':
    print("🚀 Запуск сервера...")
    print("📱 Открой в браузере: http://127.0.0.1:5000")
    print("👑 Админ: admin / admin123")
    app.run(debug=True)
