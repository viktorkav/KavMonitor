# -*- coding: utf-8 -*-
"""
Generate a static digest from Reddit threads and RSS headlines.

The script can rank popular posts, ask Gemini for editor picks and
headline translations, and write a static HTML report.
"""

import os
import sys
import json
import re
import html
import logging
import datetime
import time
import signal
import subprocess
import traceback
import fcntl
import atexit
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import yaml
import praw
import feedparser
from google import genai
from google.genai import types
from jinja2 import Environment, FileSystemLoader, select_autoescape
from dotenv import load_dotenv

# --- SETUP ---
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("digest_monitor")

with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

GAMING_SUBS = CONFIG['subreddits']['gaming']
TECH_SUBS = CONFIG['subreddits']['tech']
ALL_SUBS = GAMING_SUBS + TECH_SUBS
GIANTS = set(CONFIG['subreddits'].get('giants', []))
RSS_FEEDS = CONFIG.get('rss_feeds', [])
AI_CONFIG = CONFIG.get('ai', {})
OUTPUT_DIR = CONFIG['output'].get('directory', 'generated_output')
EDITORS_PICKS_COUNT = AI_CONFIG.get('editors_picks', 4)
AI_REQUEST_TIMEOUT_SECONDS = int(AI_CONFIG.get('request_timeout_seconds', 70))
AI_TRANSLATION_TIMEOUT_SECONDS = int(AI_CONFIG.get('translation_timeout_seconds', 45))

_lock_fp = None


def acquire_lock():
    """Acquire an exclusive lock to prevent duplicate monitor runs."""
    global _lock_fp
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor.lock")
    _lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Outra instância do monitor.py já está rodando. Saindo.")
        _lock_fp.close()
        _lock_fp = None
        sys.exit(0)
    _lock_fp.write(str(os.getpid()))
    _lock_fp.flush()
    atexit.register(release_lock)


def release_lock():
    """Release the exclusive lock."""
    global _lock_fp
    if _lock_fp:
        try:
            fcntl.flock(_lock_fp, fcntl.LOCK_UN)
            _lock_fp.close()
        except Exception:
            pass
        _lock_fp = None


# ─────────────────────────────────────────────
# REDDIT
# ─────────────────────────────────────────────

def init_reddit():
    """Initialize Reddit API client."""
    cid = os.getenv('REDDIT_CLIENT_ID')
    csecret = os.getenv('REDDIT_CLIENT_SECRET')
    if not cid or not csecret:
        log.error("Reddit credentials missing from .env")
        return None
    try:
        user_agent = os.getenv('REDDIT_USER_AGENT', 'reddit-feed-digest/1.0')
        return praw.Reddit(
            client_id=cid,
            client_secret=csecret,
            user_agent=user_agent
        )
    except Exception as e:
        log.error(f"Reddit auth failed: {e}")
        return None


def get_top_comment(submission):
    """Get the single best comment from a submission."""
    try:
        submission.comments.replace_more(limit=0)
        comments = submission.comments.list()
        if not comments:
            return None
        best = max(comments, key=lambda c: c.score)
        body = best.body.replace('\n', ' ').strip()
        return {
            'author': best.author.name if best.author else "[deleted]",
            'score': best.score,
            'body': body[:400] + "..." if len(body) > 400 else body
        }
    except Exception as e:
        log.debug(f"get_top_comment failed: {e}\n{traceback.format_exc()}")
        return None


def get_top_comments(submission, limit=10):
    """Get multiple top comments for AI context."""
    try:
        submission.comments.replace_more(limit=0)
        comments = sorted(submission.comments.list(), key=lambda c: c.score, reverse=True)[:limit]
        return [
            {
                'author': c.author.name if c.author else "[deleted]",
                'score': c.score,
                'body': c.body.replace('\n', ' ').strip()[:300]
            }
            for c in comments
        ]
    except Exception as e:
        log.debug(f"get_top_comments failed: {e}\n{traceback.format_exc()}")
        return []


def sanitize_url(url):
    """Allow only absolute http(s) URLs in rendered HTML."""
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None

    if not parsed.netloc:
        return None

    return url


def render_selftext_html(text):
    """Render selftext as escaped paragraphs instead of trusting Reddit HTML."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    paragraphs = []
    for block in re.split(r"\n\s*\n+", cleaned):
        escaped = html.escape(block.strip()).replace("\n", "<br>")
        if escaped:
            paragraphs.append(f"<p>{escaped}</p>")

    return "".join(paragraphs)


def extract_media_html(post):
    """Extract media embed HTML from a Reddit post."""
    try:
        if hasattr(post, 'is_video') and post.is_video:
            video_url = (
                post.media.get('reddit_video', {}).get('fallback_url', '')
                if isinstance(post.media, dict) else ''
            )
            safe_video_url = sanitize_url(video_url.replace("?source=fallback", ""))
            if safe_video_url:
                escaped_url = html.escape(safe_video_url, quote=True)
                return f'<video controls playsinline src="{escaped_url}" class="post-media"></video>'

        if post.domain and ('youtube.com' in post.domain or 'youtu.be' in post.domain):
            match = re.search(r"(?:v=|\/|embed\/|watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})", post.url)
            if match:
                video_id = html.escape(match.group(1), quote=True)
                title = html.escape(post.title or "YouTube video", quote=True)
                return (
                    '<div class="yt-embed">'
                    f'<iframe src="https://www.youtube-nocookie.com/embed/{video_id}?rel=0" '
                    f'title="{title}" frameborder="0" allowfullscreen></iframe>'
                    '</div>'
                )

        if hasattr(post, 'is_gallery') and post.is_gallery and hasattr(post, 'media_metadata'):
            first = next(iter(post.media_metadata))
            if post.media_metadata[first]['e'] == 'Image':
                image_url = post.media_metadata[first]['s']['u'].replace('&amp;', '&')
                safe_image_url = sanitize_url(image_url)
                if safe_image_url:
                    escaped_url = html.escape(safe_image_url, quote=True)
                    return f'<img src="{escaped_url}" alt="Gallery" class="post-media">'

        if post.url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            safe_image_url = sanitize_url(post.url)
            if safe_image_url:
                escaped_url = html.escape(safe_image_url, quote=True)
                return f'<img src="{escaped_url}" alt="Image" class="post-media">'

        if hasattr(post, 'preview') and 'images' in post.preview:
            preview_url = html.unescape(post.preview['images'][0]['resolutions'][-1]['url'])
            safe_preview_url = sanitize_url(preview_url)
            if safe_preview_url:
                escaped_url = html.escape(safe_preview_url, quote=True)
                return f'<img src="{escaped_url}" alt="Preview" class="post-media">'

        if not post.is_self:
            domain = html.escape(post.domain or "External")
            return f'<div class="ext-link-badge"><span class="ext-icon">↗</span><span class="ext-domain">{domain}</span></div>'
    except Exception as e:
        log.debug(f"extract_media_html failed: {e}\n{traceback.format_exc()}")
    return ""


def categorize_sub(sub_name):
    """Return 'gaming' or 'tech' based on config."""
    if sub_name in GAMING_SUBS:
        return 'gaming'
    return 'tech'


def _scan_single_sub(reddit, sub_name, max_age):
    """Scan a single subreddit. Designed to run in a thread."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).timestamp()
    results = []

    try:
        subreddit = reddit.subreddit(sub_name)
        raw = list(subreddit.top(time_filter='day', limit=15))

        valid = []
        seen_in_sub = set()
        for p in raw:
            if len(valid) >= 5:
                break
            if p.id in seen_in_sub or p.stickied:
                continue
            if (now_utc - p.created_utc) > max_age:
                continue
            valid.append(p)
            seen_in_sub.add(p.id)

        if not valid:
            return sub_name, []

        scores = [p.score for p in valid]
        median = sorted(scores)[len(scores) // 2] if scores else 1
        median = max(median, 5)

        category = categorize_sub(sub_name)

        for p in valid:
            top_comment = get_top_comment(p)

            results.append({
                'id': p.id,
                'subreddit': sub_name,
                'category': category,
                'title': p.title,
                'score': p.score,
                'comments': p.num_comments,
                'created_utc': p.created_utc,
                'permalink': p.permalink,
                'url': p.url,
                'url_original': f"https://reddit.com{p.permalink}",
                'relative_score': p.score / median,
                'media_html': extract_media_html(p),
                'selftext': p.selftext if p.is_self else "",
                'selftext_rendered': render_selftext_html(p.selftext if p.is_self else ""),
                'top_comment': top_comment,
                'is_video': getattr(p, 'is_video', False),
                'domain': getattr(p, 'domain', None),
                'thumbnail': getattr(p, 'thumbnail', None),
                '_post_obj': p
            })

        return sub_name, results
    except Exception as e:
        log.error(f"  r/{sub_name}: ERROR - {e}")
        return sub_name, []


def scan_subreddits(reddit):
    """
    Scan all subreddits in PARALLEL using ThreadPoolExecutor.
    Each sub runs in its own thread for ~3-4x speedup.
    """
    max_age = 24 * 3600
    all_posts = []
    seen_ids = set()

    log.info(f"Scanning {len(ALL_SUBS)} subreddits (parallel)...")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_scan_single_sub, reddit, sub, max_age): sub
            for sub in ALL_SUBS
        }

        for future in as_completed(futures):
            sub_name, posts = future.result()
            # Deduplicate across subs
            new_posts = []
            for p in posts:
                if p['id'] not in seen_ids:
                    seen_ids.add(p['id'])
                    new_posts.append(p)
            all_posts.extend(new_posts)
            if new_posts:
                log.info(f"  r/{sub_name}: {len(new_posts)} posts [{new_posts[0]['category']}]")

    log.info(f"Total posts captured: {len(all_posts)}")
    return all_posts


# ─────────────────────────────────────────────
# RSS FEEDS (Parallel)
# ─────────────────────────────────────────────

def _fetch_single_feed(feed_cfg):
    """Fetch a single RSS feed. Designed for parallel execution."""
    name = feed_cfg['name']
    url = feed_cfg['url']
    articles = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:
            title = entry.get('title', '').strip()
            link = entry.get('link', '')
            pub_date = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_date = datetime.datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                pub_date = datetime.datetime(*entry.updated_parsed[:6])

            if title and link:
                articles.append({
                    'source': name,
                    'title': title,
                    'url': link,
                    'date': pub_date,
                    'date_str': pub_date.strftime('%H:%M') if pub_date else ''
                })
        log.info(f"  RSS {name}: {len(articles)} articles")
    except Exception as e:
        log.error(f"  RSS {name}: ERROR - {e}")
    return articles


def fetch_rss_feeds():
    """Fetch all RSS feeds in PARALLEL."""
    all_articles = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_fetch_single_feed, cfg) for cfg in RSS_FEEDS]
        for future in as_completed(futures):
            all_articles.extend(future.result())

    all_articles.sort(key=lambda x: x['date'] or datetime.datetime.min, reverse=True)
    return all_articles[:15]


# ─────────────────────────────────────────────
# TRANSLATION HELPER
# ─────────────────────────────────────────────

@contextmanager
def _timeout_guard(seconds):
    """
    Abort blocking AI calls after N seconds (Linux/macOS).
    On unsupported platforms, runs without timeout.
    """
    if seconds <= 0 or os.name == 'nt':
        yield
        return

    def _handle_timeout(signum, frame):  # pragma: no cover
        raise TimeoutError(f"AI call timed out after {seconds}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _safe_generate_content(client, model, contents, config, timeout_seconds, label):
    """Call Gemini with a hard timeout and return None on timeout."""
    try:
        with _timeout_guard(timeout_seconds):
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
    except TimeoutError:
        log.warning(f"  AI: timeout em {timeout_seconds}s ({label}/{model})")
        return None

def translate_titles_batch(posts, client):
    """
    Translate Reddit post titles from English to PT-BR in a single AI call.
    Uses the same Gemini client for efficiency.
    """
    if not posts:
        return {}

    titles_input = "\n".join(f'{p["id"]}|{p["title"]}' for p in posts)

    prompt = f"""Traduza os títulos abaixo do inglês para o português brasileiro.
Mantenha o tom informativo e natural. Não traduza nomes próprios, siglas ou termos técnicos consagrados.

Formato de entrada: ID|Título em inglês
Formato de saída (JSON): {{"id": "título traduzido"}}

TÍTULOS:
{titles_input}

Retorne APENAS o JSON válido."""

    models = [
        AI_CONFIG.get('gemini', {}).get('model', 'gemini-3-flash-preview'),
        'gemini-2.5-flash',
        'gemini-2.0-flash'
    ]

    for model in models:
        try:
            response = _safe_generate_content(
                client=client,
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json"
                ),
                timeout_seconds=AI_TRANSLATION_TIMEOUT_SECONDS,
                label="translate_titles"
            )
            if response is None:
                continue
            text = re.sub(r"^```json", "", response.text, flags=re.MULTILINE)
            text = re.sub(r"^```", "", text, flags=re.MULTILINE)
            return json.loads(text)
        except Exception as e:
            if any(code in str(e) for code in ["429", "503", "RESOURCE_EXHAUSTED"]):
                continue
            log.warning(f"  Translation failed ({model}): {e}")
            continue

    return {}


# ─────────────────────────────────────────────
# SELECTIONS
# ─────────────────────────────────────────────

def select_editors_picks(posts, count=4):
    """Select top posts for AI-enhanced Editor's Picks."""
    by_score = sorted(posts, key=lambda x: x['score'], reverse=True)

    def is_media_only(p):
        has_media = any(tag in str(p['media_html']) for tag in ['video', 'img', 'iframe']) or p['is_video']
        no_text = len(p.get('selftext', '')) < 50
        return has_media and no_text

    picked = []
    used_subs = set()

    for p in by_score:
        if len(picked) >= count:
            break
        if p['subreddit'] in used_subs:
            continue
        if is_media_only(p):
            continue
        picked.append(p)
        used_subs.add(p['subreddit'])

    if len(picked) < count:
        for p in by_score:
            if len(picked) >= count:
                break
            if p['id'] in {x['id'] for x in picked}:
                continue
            if is_media_only(p):
                continue
            picked.append(p)

    return picked


def select_trending(posts, count=8, exclude_ids=None):
    """Select trending posts purely by numbers."""
    exclude = exclude_ids or set()
    eligible = [p for p in posts if p['id'] not in exclude]
    ranked = sorted(eligible, key=lambda x: x['score'] + x['comments'] * 2, reverse=True)
    return ranked[:count]


# ─────────────────────────────────────────────
# AI — EDITOR'S PICKS (PT-BR)
# ─────────────────────────────────────────────

def enrich_for_ai(picks):
    """Fetch extra comment context for AI processing."""
    enriched = []
    for item in picks:
        post = item['_post_obj']
        comments = get_top_comments(post, limit=8)
        enriched.append({**item, 'comments_context': comments})
    return enriched


def generate_ai_picks(enriched_posts):
    """Use Gemini to generate journalistic titles and summaries in PT-BR."""
    if not enriched_posts:
        return []

    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key:
        log.error("GOOGLE_API_KEY missing — skipping AI")
        return []

    context = ""
    for i, p in enumerate(enriched_posts, 1):
        comments_text = "\n".join(
            f"  - [{c['author']}] ({c['score']}↑): {c['body']}"
            for c in p.get('comments_context', [])
        )
        context += f"""
POST {i}:
  ID: {p['id']}
  Subreddit: r/{p['subreddit']} ({p['category']})
  Score: {p['score']} | Comments: {p['comments']}
  Original Title: {p['title']}
  Body: {p['selftext'][:500] if p.get('selftext') else '(Link post)'}
  Top Comments:
{comments_text}
  ---
"""

    system_prompt = """Você é o editor-chefe de um briefing premium de tecnologia e games.
Sua missão: reescrever títulos e resumos de posts do Reddit como jornalismo profissional em PORTUGUÊS BRASILEIRO.

REGRAS DE ESTILO:
1. Manchetes devem ser FACTUAIS, OBJETIVAS e INFORMATIVAS. Escreva como a Folha de São Paulo ou o G1, NÃO como YouTuber.
   - BOM: "Valve confirma que Steam Deck 2 está em desenvolvimento ativo"
   - RUIM: "Steam Deck 2 vem aí e vai ser INCRÍVEL!"
2. Resumos: 1-2 frases no máximo. Explique o fato e adicione contexto dos comentários se relevante.
   - Tom neutro e informativo. Sem hype, sem exclamações.
3. Tags: 3-4 tags relevantes ao assunto (sem # no prefixo).
4. Manter nomes próprios, siglas e termos técnicos consagrados em inglês quando faz mais sentido (ex: Steam Deck, PlayStation, GPU, CPU).

SAÍDA: Retorne APENAS JSON válido:
[
  {
    "id": "ID_DO_POST",
    "headline": "Manchete jornalística em PT-BR",
    "summary": "Resumo conciso em PT-BR.",
    "tags": ["Tag1", "Tag2", "Tag3"]
  }
]"""

    models = [
        AI_CONFIG.get('gemini', {}).get('model', 'gemini-3-flash-preview'),
        'gemini-2.5-flash',
        'gemini-2.0-flash'
    ]

    client = genai.Client(api_key=api_key)

    for model_name in models:
        try:
            log.info(f"  AI: Gerando com {model_name}...")
            response = _safe_generate_content(
                client=client,
                model=model_name,
                contents=f"{system_prompt}\n\nPOSTS:\n{context}",
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json"
                ),
                timeout_seconds=AI_REQUEST_TIMEOUT_SECONDS,
                label="editors_picks"
            )
            if response is None:
                continue
            text = response.text
            text = re.sub(r"^```json", "", text, flags=re.MULTILINE)
            text = re.sub(r"^```", "", text, flags=re.MULTILINE)

            data = json.loads(text)
            log.info(f"  AI: Sucesso — {len(data)} itens gerados")
            return data, client  # Return client for reuse in translation

        except Exception as e:
            if any(code in str(e) for code in ["429", "503", "RESOURCE_EXHAUSTED"]):
                log.warning(f"  AI: {model_name} rate-limited, tentando próximo...")
                continue
            log.error(f"  AI: {model_name} erro — {e}")
            continue

    log.error("  AI: Todos os modelos falharam")
    return [], None


def merge_ai_data(picks, ai_data):
    """Merge AI-generated headlines/summaries into picks."""
    ai_map = {str(item['id']): item for item in ai_data}

    for pick in picks:
        pid = str(pick['id'])
        if pid in ai_map:
            ai = ai_map[pid]
            pick['ai_headline'] = ai.get('headline', pick['title'])
            pick['ai_summary'] = ai.get('summary', '')
            pick['ai_tags'] = ai.get('tags', [])
        else:
            pick['ai_headline'] = pick['title']
            pick['ai_summary'] = pick.get('selftext', '')[:200]
            pick['ai_tags'] = []

    return picks


# ─────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────

def format_timestamp(value):
    return datetime.datetime.fromtimestamp(value).strftime('%H:%M')


def format_number(value):
    try:
        n = int(value)
        if n >= 1000:
            return f"{n/1000:.1f}k"
        return str(n)
    except Exception:
        return str(value)


def render_report(picks, trending, rss_articles, all_posts):
    """Render the final HTML report."""
    env = Environment(
        loader=FileSystemLoader('.'),
        autoescape=select_autoescape(['html', 'xml'])
    )
    env.filters['timestamp_to_time'] = format_timestamp
    env.filters['fmt_number'] = format_number
    template = env.get_template('templates/report.html')

    by_sub = {}
    for p in all_posts:
        sub = p['subreddit']
        if sub not in by_sub:
            by_sub[sub] = []
        by_sub[sub].append(p)

    by_sub = dict(sorted(by_sub.items()))
    for sub in by_sub:
        by_sub[sub].sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.date.today().strftime("%d %b %Y")

    html_output = template.render(
        today=today_str,
        picks=picks,
        trending=trending,
        rss_articles=rss_articles,
        by_sub=by_sub,
        generation_time=datetime.datetime.now().strftime("%H:%M")
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    archive = os.path.join(OUTPUT_DIR, f"report_{datetime.date.today().strftime('%Y-%m-%d')}.html")
    with open(archive, 'w', encoding='utf-8') as f:
        f.write(html_output)
    log.info(f"Salvo: {archive}")

    latest = os.path.join(OUTPUT_DIR, "index.html")
    with open(latest, 'w', encoding='utf-8') as f:
        f.write(html_output)
    log.info(f"Salvo: {latest}")

    return latest


# ─────────────────────────────────────────────
# OPTIONAL PUBLISH STEP
# ─────────────────────────────────────────────

def publish_report(local_file):
    """Run an optional post-render publish command."""
    if not local_file or not os.path.isfile(local_file):
        log.error(f"Publish step: report artifact missing ({local_file})")
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    publish_command = os.getenv("PUBLISH_COMMAND", "").strip()
    if not publish_command:
        log.info("Publish step skipped (PUBLISH_COMMAND not set)")
        return

    try:
        log.info("Publish step: running configured command")
        publish_env = os.environ.copy()
        publish_env["REPORT_FILE"] = os.path.abspath(local_file)
        publish_env["REPORT_DIR"] = script_dir
        result = subprocess.run(
            publish_command,
            cwd=script_dir,
            env=publish_env,
            shell=True,
            timeout=180,
        )
        if result.returncode == 0:
            log.info("Publish step: OK")
        else:
            log.error(f"Publish step: failed (exit {result.returncode})")
    except subprocess.TimeoutExpired:
        log.error("Publish step: timed out (180s)")
    except Exception as e:
        log.error(f"Publish step: failed - {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    acquire_lock()
    start_time = time.time()

    log.info("=" * 50)
    log.info("Reddit Feed Digest")
    log.info("=" * 50)


    # 1. Reddit (parallel scanning)
    reddit = init_reddit()
    if not reddit:
        sys.exit(1)

    all_posts = scan_subreddits(reddit)
    if not all_posts:
        log.error("Nenhum post encontrado. Encerrando.")
        sys.exit(1)

    # 2. Editor's Picks (AI-enhanced, PT-BR)
    log.info(f"\n--- Editor's Picks ({EDITORS_PICKS_COUNT}) ---")
    picks = select_editors_picks(all_posts, count=EDITORS_PICKS_COUNT)
    enriched = enrich_for_ai(picks)
    ai_result = generate_ai_picks(enriched)
    ai_data, gemini_client = ai_result if isinstance(ai_result, tuple) else (ai_result, None)
    picks = merge_ai_data(picks, ai_data)

    pick_ids = {p['id'] for p in picks}

    # 3. Trending (pure numbers)
    log.info("\n--- Trending por Números ---")
    trending = select_trending(all_posts, count=8, exclude_ids=pick_ids)
    log.info(f"Selecionados {len(trending)} posts trending")

    # 4. Translate trending titles to PT-BR
    if gemini_client and trending:
        log.info("  Traduzindo títulos para PT-BR...")
        translations = translate_titles_batch(trending, gemini_client)
        for t in trending:
            if t['id'] in translations:
                t['title_ptbr'] = translations[t['id']]

    # 5. RSS Feeds (parallel)
    log.info("\n--- RSS Feeds ---")
    rss_articles = fetch_rss_feeds()
    log.info(f"Total RSS: {len(rss_articles)} artigos")

    # 6. Render
    log.info("\n--- Renderizando ---")
    output_file = render_report(picks, trending, rss_articles, all_posts)

    # 7. Optional publish step
    log.info("\n--- Publish ---")
    publish_report(output_file)

    elapsed = time.time() - start_time
    log.info(f"\n✅ Concluído em {elapsed:.0f}s")

    # Write health check file so external monitors can verify the cron is running
    health_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_run")
    try:
        with open(health_file, "w") as f:
            json.dump({
                "status": "ok",
                "timestamp": datetime.datetime.now().isoformat(),
                "elapsed_seconds": round(elapsed),
                "posts": len(all_posts),
                "picks": len(picks),
                "trending": len(trending),
                "rss": len(rss_articles),
            }, f)
    except Exception:
        pass


if __name__ == "__main__":
    main()
