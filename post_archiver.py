# script for archiving tumblr posts, designed to run in a github actions workflow.
# mostly uses the NPF (new post format) content parser for ease of api access
# legacy fallbacks for all content types in the works
#
# every run: fetch new posts -> save the raw api data as posts/<id>.json -> mirror images to the bucket
# -> rebuild every post's html from its raw data -> write posts.json for the frontend.
# the raw json is the real archive and the html is just a view of it, so a parser fix applies to every post on the next run.

import os
import re
import json
import time
import hashlib
import mimetypes
import requests
import html
from datetime import datetime, timezone
from urllib.parse import urlparse
from dotenv import load_dotenv

# --- configuration ---
load_dotenv()

# these should be set as environment secrets/variables in the github repository.
TUMBLR_API_KEY = os.environ.get("TUMBLR_API_KEY")
BLOG_IDENTIFIER = os.environ.get("BLOG_IDENTIFIER")
TAGS_STRING = os.environ.get("TAGS_TO_ARCHIVE", "")
TAGS_TO_ARCHIVE = [tag.strip() for tag in TAGS_STRING.split(',') if tag.strip()]

# the list of my blogs lives in the frontend repo. this can also be a local file path.
BLOGS_JSON = os.environ.get("BLOGS_JSON") or "https://raw.githubusercontent.com/wildwizardcreation/archive-wildwizardcreation/main/public/data/blogs/blogs.json"

# backfill pages through a blog's whole history, and re-fetches archived posts that have no raw json yet.
BACKFILL = os.environ.get("BACKFILL", "").strip().lower() in ("1", "true", "yes")

# the panic button. "tumblr" keeps tumblr's urls as every image's src, "mirror" swaps them all for the bucket copies.
MEDIA_SOURCE = (os.environ.get("MEDIA_SOURCE") or "tumblr").strip().lower()
MEDIA_BASE_URL = "https://media.wildwizardcreation.com"
B2_KEY_ID = os.environ.get("B2_KEY_ID")
B2_APP_KEY = os.environ.get("B2_APP_KEY")
B2_BUCKET = os.environ.get("B2_BUCKET")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT")

# the base directory where all blog archives will be stored.
BASE_OUTPUT_DIR = "blogs"

# shared lookups, filled in once the run knows what's on disk.
MY_BLOGS = []    # everything in blogs.json
POST_INDEX = {}  # post id -> the archive folder it's filed under
ID_RANGES = {}   # archive folder -> (lowest, highest) archived post id
MEDIA = {}       # the media.json of the blog currently being built

# --- html template ---
# defines the structure for each archived post's html file.
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>{meta}
</head>
<body>
    <div class="container">
        <main class="archive-post"{attributes}>
            {content}
        </main>
        <footer class="post-meta">
            {metadata}
        </footer>
    </div>
</body>
</html>
"""

# --- blog list helpers ---

def tumblr_name(name):
    # some deactivated blogs get renamed to things like "wildwizardcreation-deactivated2"
    return re.sub(r'-deactivated\d*$', '', str(name or '').strip().lower())

def load_blogs():
    # reads blogs.json. blogs with stripNumerator share a tumblr url, the "-1" / "-2" is only the archive's folder name.
    try:
        if BLOGS_JSON.startswith('http'):
            response = requests.get(BLOGS_JSON, timeout=30)
            response.raise_for_status()
            blogs = response.json()
        else:
            with open(BLOGS_JSON, encoding='utf-8') as f: blogs = json.load(f)
    except Exception as e:
        print(f"!!! couldn't load blogs.json ({e}). links to my own posts and cross-blog info will be skipped this run.")
        return []

    for blog in blogs:
        blog['tumblr'] = (re.sub(r'-\d+$', '', blog['id']) if blog.get('stripNumerator') else blog['id']).lower()
        blog['live'] = str(blog.get('endDate') or 'present').strip().lower() == 'present'
    return blogs

def is_my_blog(name):
    return any(blog['tumblr'] == tumblr_name(name) for blog in MY_BLOGS)

def block_blog_name(block):
    # deactivated blogs in the trail only carry a broken blog name
    return block.get('blog_name') or (block.get('blog') or {}).get('name') or block.get('broken_blog_name')

def post_id_of(post):
    return str(post.get('id_string') or post['id'])

# --- formatting helper functions ---

def format_datetime_for_display(timestamp):
    # formats a unix timestamp into a more readable 'month day, year · hour:minute am/pm' string, always in utc.
    if not timestamp:
        return "???"
    try:
        dt_obj = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        formatted_time = dt_obj.strftime("%I:%M %p")
        if formatted_time.startswith('0'):
            formatted_time = formatted_time[1:]
        return dt_obj.strftime(f"%B %d, %Y · {formatted_time} UTC")
    except (ValueError, TypeError, OverflowError, OSError):
        return "???"

def fix_arrows(text):
    # tumblr-esque arrow replacements, done after escaping
    return text.replace("&lt;-", "\U0001F850").replace("-&gt;", "\U0001F852")

def format_op_header(username, date_str):
    # formats the header for the original post
    return f'<div class="op-block">\n  <p class="user-info">{html.escape(username, quote=False)}</p>\n  <p class="posted">Posted · <span>{date_str}</span></p>\n</div>'

def format_op_content(op_html):
    # formats the container for the main op content
    return f'<div class="op-content">\n{op_html.strip()}\n</div>\n'

def format_reblog_header(username, reblogged_from, date_str):
    # the "x reblogged y" line at the top of every reblog, so one that only added tags can't be mistaken for an original post
    from_html = f' <span class="reblogged-from">{html.escape(reblogged_from, quote=False)}</span>' if reblogged_from else ''
    return f'<div class="reblog-header">\n  <p><span class="user-info">{html.escape(username, quote=False)}</span> reblogged{from_html} · <span>{date_str}</span></p>\n</div>\n'

def format_reblog_block(username, date_str, reblog_html):
    # formats a reblog block
    return f'<div class="reblog-block">\n  <p class="user-info">{html.escape(username, quote=False)}</p>\n  <p class="reblogged">Reblogged · <span>{date_str}</span></p>\n  <div class="reblog-content">{reblog_html.strip()}</div>\n</div>'

def format_ask_block(asker, question_html):
    # formats an ask block
    return f'<div class="ask-block">\n  <p class="asker"><span>{html.escape(asker, quote=False)}</span> asked:</p>\n  <div class="question">{question_html}</div>\n</div>'

def format_answer_block(username, answer_html, include_header=True):
    # formats an answer block. optional "[user] answered" header for reblogged asks
    header_html = f'  <p class="answerer"><span>{html.escape(username, quote=False)}</span> answered:</p>\n' if include_header else ''
    return f'<div class="answer-block">\n{header_html}  <div class="answer-content">{answer_html.strip()}</div>\n</div>'

# --- links to my own posts ---

def post_link_target(url):
    # pulls (blog name, post id) out of the usual tumblr post url shapes
    try: parsed = urlparse(url)
    except ValueError: return None
    host, path = (parsed.netloc or '').lower(), parsed.path or ''

    if host in ('tumblr.com', 'www.tumblr.com'):
        match = re.match(r'^/blog/view/([^/]+)/(\d+)', path) or re.match(r'^/([^/]+)/(\d+)(?:/|$)', path)
        return (match.group(1), match.group(2)) if match else None
    if host.endswith('.tumblr.com'):
        match = re.match(r'^/post/(\d+)', path)
        return (host.split('.')[0], match.group(1)) if match else None
    return None

def closest_blog_by_id(candidates, post_id):
    # tumblr ids only go up over time, so the blog whose archived id range sits closest to this id is the best guess
    best, best_distance = candidates[0], None
    for blog in candidates:
        id_range = ID_RANGES.get(blog['id'])
        if not id_range: continue
        distance = 0 if id_range[0] <= post_id <= id_range[1] else min(abs(post_id - id_range[0]), abs(post_id - id_range[1]))
        if best_distance is None or distance < best_distance:
            best, best_distance = blog, distance
    return best

def archive_link_for(url):
    # where a link to one of my own posts should point in the archive. returns None for links that should be left alone.
    target = post_link_target(url)
    if not target: return None
    blog_name, post_id = target

    # the id is looked up across every blog instead of trusting the name in the url,
    # since two of my blogs shared a url and posts can move
    if post_id in POST_INDEX:
        return f'/blogs/{POST_INDEX[post_id]}/post/{post_id}/'

    # not archived. a blog that's gone still gets pointed at the archive, which shows the unarchived page.
    # a blog that's still up keeps its tumblr link, and other people's posts are never touched.
    ended = [blog for blog in MY_BLOGS if blog['tumblr'] == tumblr_name(blog_name) and not blog['live']]
    if not ended: return None
    return f'/blogs/{closest_blog_by_id(ended, int(post_id))["id"]}/post/{post_id}/'

def link_attributes(url):
    # the href for a link in a post. wayback's wrapper is stripped first (posts exported from wayback captures have every
    # link pointed at the capture), then links to my own posts are sent to the archive with the original url kept alongside.
    url = clean_wayback_url(url) or '#'
    archive_url = archive_link_for(url)
    if archive_url:
        return f'href="{archive_url}" data-original-href="{html.escape(url, quote=True)}"'
    return f'href="{html.escape(url, quote=True)}"'

def link_open_tag(url):
    return f'<a {link_attributes(url)}>'

ANCHOR_TAG = re.compile(r'<a\b[^>]*>', re.I)

def refresh_exported_link(match):
    # one <a> in a post that only exists as html. same treatment as links in posts built from json,
    # read from data-original-href when it was already rewritten so running this again changes nothing.
    tag = match.group(0)
    source = re.search(r'\bdata-original-href="([^"]*)"', tag) or re.search(r'\shref="([^"]*)"', tag)
    if not source: return tag
    stripped = re.sub(r'\s+(?:href|data-original-href)="[^"]*"', '', tag)
    return re.sub(r'^<a\b', lambda _: f'<a {link_attributes(html.unescape(source.group(1)))}', stripped, count=1, flags=re.I)

# --- media helpers ---

def media_filename(url):
    return os.path.basename(urlparse(url).path)

def media_attributes(url, attr='src'):
    # the url stays on tumblr with the mirrored copy alongside it as a fallback, unless MEDIA_SOURCE flips everything to the bucket.
    # images, audio and video use src. a video's poster gets its own pair (data-mirror-poster / data-tumblr-poster).
    suffix = '' if attr == 'src' else f'-{attr}'
    entry = MEDIA.get(url)
    if not entry or entry.get('status') != 'ok':
        return f'{attr}="{html.escape(url, quote=True)}"'
    mirror_url = html.escape(f"{MEDIA_BASE_URL}/{entry['key']}", quote=True)
    if MEDIA_SOURCE == 'mirror':
        return f'{attr}="{mirror_url}" data-tumblr-{attr}="{html.escape(url, quote=True)}"'
    return f'{attr}="{html.escape(url, quote=True)}" data-mirror{suffix}="{mirror_url}"'

# --- layout-aware content parser ---
# this section reconstructs post layouts, including image galleries and "read more" breaks as accurately as possible

def format_text(raw_text, formatting):
    # turns npf formatting ranges into inline html. ranges can overlap (bold 0-10, italic 5-15),
    # so at every boundary the tags that end there are closed, along with anything opened after them, which then gets reopened.
    spans = []
    for fmt in formatting or []:
        start, end, fmt_type = fmt.get('start'), fmt.get('end'), fmt.get('type')
        if start is None or end is None: continue
        start, end = max(0, start), min(len(raw_text), end)
        if end <= start: continue

        tags = {'bold': ('<strong>', '</strong>'), 'italic': ('<em>', '</em>'), 'small': ('<small>', '</small>'),
                'strikethrough': ('<s>', '</s>')}
        if fmt_type == 'link':
            tags['link'] = (link_open_tag(fmt.get('url', '#')), '</a>')
        elif fmt_type == 'color' and re.fullmatch(r'#[0-9a-fA-F]{3,8}', fmt.get('hex', '')):
            tags['color'] = (f'<span style="color: {fmt["hex"]}">', '</span>')
        # mentions are left as plain text on purpose. not everyone wants their blog linked from a public archive.
        if fmt_type in tags:
            spans.append((start, end, tags[fmt_type]))

    boundaries = sorted({0, len(raw_text)} | {point for span in spans for point in span[:2]})
    text_parts, open_spans = [], []
    for position, next_position in zip(boundaries, boundaries[1:] + [None]):
        if any(span[1] <= position for span in open_spans):
            reopen = []
            while any(span[1] <= position for span in open_spans):
                span = open_spans.pop()
                text_parts.append(span[2][1])
                if span[1] > position: reopen.insert(0, span)
            for span in reopen:
                text_parts.append(span[2][0])
                open_spans.append(span)
        # longest first, so shorter ranges nest inside
        for span in sorted((s for s in spans if s[0] == position), key=lambda s: -s[1]):
            text_parts.append(span[2][0])
            open_spans.append(span)
        if next_position is not None:
            text_parts.append(html.escape(raw_text[position:next_position], quote=False))

    return fix_arrows("".join(text_parts))

def format_embed(block, kind):
    # video and audio blocks. tumblr-hosted files play from their media url, other providers (youtube, spotify etc)
    # get their embed url in an iframe, and anything without either falls back to a plain link.
    media = block.get('media')
    if isinstance(media, list): media = media[0] if media else None
    source = (media or {}).get('url') or block.get('url')
    embed_url = block.get('embed_url')
    is_tumblr_file = bool(source) and (block.get('provider') == 'tumblr' or str((media or {}).get('type', '')).startswith(f'{kind}/'))

    if is_tumblr_file:
        poster = block.get('poster') or []
        poster_attr = f' {media_attributes(poster[0]["url"], "poster")}' if kind == 'video' and poster and poster[0].get('url') else ''
        return f'<{kind} controls preload="metadata" {media_attributes(source)}{poster_attr}></{kind}>\n'
    if embed_url:
        return f'<div class="{kind}-embed"><iframe src="{html.escape(embed_url, quote=True)}" loading="lazy" allowfullscreen></iframe></div>\n'
    if source:
        label = block.get('title') or f"{block.get('provider') or kind} link"
        return f'<p class="{kind}-link"><a href="{html.escape(source, quote=True)}">{html.escape(label, quote=False)}</a></p>\n'
    return ''

def _process_single_block(block, active_list_type_ref, polls=None):
    # helper to process one content block. modifies active_list_type_ref for list tracking.
    # polls holds the saved results for any polls in the post.
    block_html, media = '', []
    active_list_type = active_list_type_ref[0]
    block_type, subtype = block.get('type'), block.get('subtype')

    is_list_item = subtype in ('ordered-list-item', 'unordered-list-item')
    if active_list_type and not is_list_item:
        block_html += f'</{active_list_type}>\n'
        active_list_type = None

    if block_type == 'text':
        text = format_text(block.get('text', ''), block.get('formatting', []))

        tag_map = {'heading1': 'h1', 'heading2': 'h2', 'quote': 'blockquote', 'indented': 'blockquote',
                   'chat': 'p class="chat-style"', 'quirky': 'p class="quirky-style"'}
        if subtype in tag_map:
            tag = tag_map[subtype]
            block_html += f'<{tag}>{text}</{tag.split()[0]}>\n' if ' ' in tag else f'<{tag}>{text}</{tag}>\n'
        elif subtype in ('unordered-list-item', 'ordered-list-item'):
            list_tag = 'ul' if 'unordered' in subtype else 'ol'
            if active_list_type != list_tag:
                if active_list_type: block_html += f'</{active_list_type}>\n'
                block_html += f'<{list_tag}>\n'
                active_list_type = list_tag
            block_html += f'<li>{text}</li>\n'
        else:
            block_html += f'<p>{text}</p>\n'

    elif block_type == 'image' and block.get('media'):
        url = block['media'][0]['url']
        alt_text = block.get('alt_text') or 'Tumblr Image'
        media.append(url)
        block_html += f'<img {media_attributes(url)} alt="{html.escape(alt_text, quote=True)}">\n'

    elif block_type in ('video', 'audio'):
        block_html += format_embed(block, block_type)

    elif block_type == 'link' and block.get('url'):
        url = clean_wayback_url(block['url'])
        title = fix_arrows(html.escape(block.get('title') or block.get('display_url') or url, quote=False))
        block_html += f'<div class="link-block">\n  <p>{link_open_tag(url)}{title}</a></p>\n'
        if block.get('description'):
            block_html += f'  <p class="link-description">{fix_arrows(html.escape(block["description"], quote=False))}</p>\n'
        block_html += '</div>\n'

    elif block_type == 'poll':
        question = block.get('question', 'Poll')
        display_answers = block.get('answers', [])

        # results are fetched while the poll is running and saved with the post, see refresh_polls
        votes_map = ((polls or {}).get(block.get('client_id')) or {}).get('results') or {}
        total_votes = sum(votes_map.values())

        block_html += '<div class="poll-block">\n'
        block_html += f'  <p class="poll-question"><strong>{fix_arrows(html.escape(question, quote=False))}</strong></p>\n'

        if display_answers:
            block_html += '  <ul class="poll-options">\n'
            for answer in display_answers:
                answer_text = answer.get('answer_text', '...')
                votes = votes_map.get(answer.get('client_id'), 0)

                percentage = 0.0
                if total_votes > 0:
                    percentage = (votes / total_votes) * 100
                percentage_str = f' <span class="poll-percentage">({percentage:.0f}%)</span>'

                block_html += (
                    f'    <li style="background: linear-gradient(to right, #f0f0f0 {percentage:.0f}%, transparent {percentage:.0f}%); '
                    f'padding: 0.25em 0.5em;" data-percentage="{percentage:.0f}">'
                    f'{fix_arrows(html.escape(answer_text, quote=False))}{percentage_str}'
                    f'</li>\n'
                )
            block_html += '  </ul>\n'
        if total_votes > 0:
            block_html += f'  <p class="poll-total-votes">{total_votes:,} votes total</p>\n'
        block_html += '</div>\n'

    active_list_type_ref[0] = active_list_type
    return {'html': block_html, 'media': media}

def parse_api_content(all_content_blocks, layout_blocks=None, indices_to_process=None, polls=None):
    # parses content and layout blocks into html, respecting content order and truncation.
    if not all_content_blocks: return {'html': '', 'media': []}
    if indices_to_process is None: indices_to_process = list(range(len(all_content_blocks)))

    indices_set = set(indices_to_process)
    media, html_before, html_after = [], '', ''
    active_list_type_ref = [None]
    rows_layout = next((l for l in layout_blocks if l.get('type') == 'rows'), None) if layout_blocks else None
    truncate_after = rows_layout.get('truncate_after') if rows_layout else None
    last_visible_idx = truncate_after if truncate_after is not None else float('inf')

    if rows_layout:
        for row_data in rows_layout.get('display', []):
            relevant_indices = [i for i in row_data.get('blocks', []) if i in indices_set]
            if not relevant_indices: continue

            current_row_html = ''
            if len(relevant_indices) > 1:
                current_row_html += f'<div class="image-row-{len(relevant_indices)}">\n'
                for index in relevant_indices:
                     result = _process_single_block(all_content_blocks[index], active_list_type_ref, polls=polls)
                     current_row_html += result['html']; media.extend(result['media'])
                current_row_html += '</div>\n'
            else:
                result = _process_single_block(all_content_blocks[relevant_indices[0]], active_list_type_ref, polls=polls)
                current_row_html += result['html']; media.extend(result['media'])

            if min(relevant_indices) > last_visible_idx: html_after += current_row_html
            else: html_before += current_row_html
    else:
        for index in indices_to_process:
            result = _process_single_block(all_content_blocks[index], active_list_type_ref, polls=polls)
            html_before += result['html']; media.extend(result['media'])

    if active_list_type_ref[0]:
        closing_tag = f'</{active_list_type_ref[0]}>\n'
        if html_after: html_after = html_after.rstrip('\n') + closing_tag
        else: html_before = html_before.rstrip('\n') + closing_tag

    final_html = html_before
    if html_after:
        final_html += f'<details>\n  <summary>Keep Reading</summary>\n{html_after}</details>\n'

    return {'html': final_html, 'media': list(dict.fromkeys(media))}

# --- post rendering ---

def post_chain(post):
    # the full reblog chain, oldest first, ending with the post itself
    return (post.get('trail') or []) + [post]

def post_type(post):
    # "reblog" for anything with a trail (a reblogged ask counts as a reblog), otherwise "ask" or "original"
    if post.get('trail'): return 'reblog'
    if post.get('type') == 'answer' or post.get('question'): return 'ask'
    if any(block.get('type') == 'ask' for block in post.get('layout', [])): return 'ask'
    return 'original'

def cross_blog_sources(post):
    # my other blogs that show up in this post's chain, like wonderous-wizard reblogging an old wildwizardcreation post
    own_name = tumblr_name(post.get('blog_name'))
    names = [tumblr_name(block_blog_name(block)) for block in post.get('trail') or [] if is_my_blog(block_blog_name(block))]
    return list(dict.fromkeys(name for name in names if name != own_name))

def post_source(post):
    # where a post came from, if it isn't its original source
    if post.get('_source'): return post['_source']
    source_blogs = cross_blog_sources(post)
    if source_blogs:
        return {'type': 'cross-blog', 'reblog': f"{post.get('blog_name')}/{post_id_of(post)}", 'blogs': source_blogs}
    return None

def render_post(post):
    # builds the full html page for one post from its raw api data.
    post_id = post_id_of(post)
    polls = post.get('_polls') or {}
    html_body = ""
    has_trail = bool(post.get('trail'))

    is_answer = post.get('type') == 'answer' or post.get('question')
    if not is_answer:
        layout_to_check = post.get('layout', []) if not has_trail else post.get('trail', [{}])[0].get('layout', [])
        if any(block.get('type') == 'ask' for block in layout_to_check):
            is_answer = True

    full_chain = post_chain(post)

    for i, block in enumerate(full_chain):
        is_root = i == 0
        is_final_reblogger = i == len(full_chain) - 1
        username = block_blog_name(block)
        content_to_parse, layout_to_use = block.get('content', []), block.get('layout', [])

        if not username or (not is_root and not content_to_parse): continue

        # every entry in the trail carries its own post's timestamp. the last entry in the chain is the post itself.
        timestamp = post.get('timestamp') if is_final_reblogger else (block.get('post') or {}).get('timestamp')

        if is_root:
            html_body += format_op_header(username, format_datetime_for_display(timestamp))

            if is_answer:
                ask_block = next((b for b in layout_to_use if b.get('type') == 'ask'), None)
                if ask_block:
                    asker = "Anonymous"
                    if 'attribution' in ask_block and isinstance(ask_block.get('attribution'), dict) and ask_block['attribution'].get('type') == 'blog':
                        asker = ask_block['attribution']['blog']['name']
                    ask_indices = ask_block.get('blocks', [])
                    answer_indices = [idx for idx in range(len(content_to_parse)) if idx not in ask_indices]
                    parsed_ask = parse_api_content(content_to_parse, layout_to_use, ask_indices, polls=polls)
                    parsed_answer = parse_api_content(content_to_parse, layout_to_use, answer_indices, polls=polls)
                    html_body += format_ask_block(asker, parsed_ask['html'])
                    html_body += format_answer_block(username, parsed_answer['html'], include_header=has_trail)
                else: # legacy ask fallback
                    parsed_content = parse_api_content(content_to_parse, layout_to_use, polls=polls)
                    question_text = fix_arrows(html.escape(post.get('question', ''), quote=False))
                    html_body += format_ask_block(post.get('asking_name', 'Anonymous'), f"<blockquote>{question_text}</blockquote>")
                    html_body += format_answer_block(username, parsed_content['html'], include_header=has_trail)
            else:
                parsed_content = parse_api_content(content_to_parse, layout_to_use, polls=polls)
                html_body += format_op_content(parsed_content['html'])
        else:
            parsed_content = parse_api_content(content_to_parse, layout_to_use, polls=polls)

            # don't create an empty block for the final reblogger if they only added tags
            if is_final_reblogger and not parsed_content['html'].strip(): continue

            html_body += format_reblog_block(username, format_datetime_for_display(timestamp), parsed_content['html'])

    blog_name = post.get('blog_name', 'N/A')
    reblogged_from = post.get('reblogged_from_name') or ''
    reblogged_root = post.get('reblogged_root_name') or ''
    if has_trail:
        html_body = format_reblog_header(blog_name, reblogged_from, format_datetime_for_display(post.get('timestamp'))) + html_body

    # the post type and where it came from sit on <main>, so the frontend can style and filter by them
    attributes = f' data-type="{post_type(post)}"'
    if has_trail:
        attributes += f' data-reblogged-from="{html.escape(reblogged_from, quote=True)}" data-reblogged-root="{html.escape(reblogged_root, quote=True)}"'

    # marks posts that aren't from their original source. the readme has the full list of what these mean.
    meta = ''
    source = post_source(post)
    if source:
        # the "user/id" pair split in two, since css can't split a string itself
        reblog_user, _, reblog_id = source["reblog"].partition('/')
        attributes += f' data-source="{source["type"]}" data-source-reblog-user="{html.escape(reblog_user, quote=True)}" data-source-reblog-id="{html.escape(reblog_id, quote=True)}"'
        meta += f'\n    <meta name="archive-source" content="{source["type"]}">'
        meta += f'\n    <meta name="archive-source-reblog" content="{html.escape(source["reblog"], quote=True)}">'
        if source.get('blogs'):
            blogs_str = html.escape(' '.join(source['blogs']), quote=True)
            attributes += f' data-source-blogs="{blogs_str}"'
            meta += f'\n    <meta name="archive-source-blogs" content="{blogs_str}">'

    metadata_html = f"<p><strong>{html.escape(blog_name, quote=False)}</strong> | post id <strong>{post_id}</strong></p>"
    metadata_html += f"<p><strong>archived:</strong> {format_datetime_for_display(post.get('_archived'))}</p>"
    if post.get('post_url'):
        metadata_html += f'<p><a href="{html.escape(post["post_url"], quote=True)}" class="tumblr-link">view on tumblr</a></p>'
    tags = post.get('tags', [])
    if tags:
        tags_html = "".join([f'<a href="#" class="tag">#{html.escape(tag, quote=False)}</a>' for tag in tags])
        metadata_html += f'<div class="tags-container">{tags_html}</div>'

    return HTML_TEMPLATE.format(title=html.escape(f"{blog_name} - post {post_id}", quote=False), meta=meta,
                                attributes=attributes, metadata=metadata_html, content=html_body.strip())

# --- raw json storage ---

def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def write_if_changed(path, text):
    # only touches the file when something is actually different, so reruns don't fill the repo with empty commits
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text: return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f: f.write(text)
    return True

def write_json(path, data):
    return write_if_changed(path, json.dumps(data, indent=1, ensure_ascii=False) + '\n')

def archived_time_from_html(path):
    # for posts archived before raw json existed, keep the archive date that's already in their footer
    try:
        with open(path, encoding='utf-8') as f: page = f.read()
        match = re.search(r'<strong>archived:</strong>\s*([^<]+?)(?: UTC)?</p>', page)
        return int(datetime.strptime(match.group(1).strip(), "%B %d, %Y · %I:%M %p").replace(tzinfo=timezone.utc).timestamp())
    except (FileNotFoundError, AttributeError, ValueError):
        return None

def save_raw_post(post, posts_dir):
    # saves a post's raw api data. the archive date and any saved poll results carry over when a post is re-fetched.
    post_id = post_id_of(post)
    path = os.path.join(posts_dir, f"{post_id}.json")
    previous = read_json(path, {})
    # things like note counts change constantly, so a saved post is only replaced when what's in it was edited
    if previous and all(previous.get(key) == post.get(key) for key in ('content', 'layout', 'trail', 'tags')): return
    post['_archived'] = previous.get('_archived') or archived_time_from_html(os.path.join(posts_dir, f"{post_id}.html")) or int(time.time())
    if previous.get('_polls'): post['_polls'] = previous['_polls']
    if write_json(path, post):
        print(f"  -> saved raw data for post {post_id}")

def ids_with(posts_dir, extension):
    if not os.path.isdir(posts_dir): return set()
    return {name[:-len(extension)] for name in os.listdir(posts_dir) if name.endswith(extension) and name[:-len(extension)].isdigit()}

# --- fetching ---

def fetch_posts(blog, extra_params, known_ids, require_tags=False):
    # pages back through a blog 20 posts at a time (the api max) until a page has nothing new. backfill keeps going to the end.
    # posts on the pages it does read get their raw data refreshed, which picks up recent edits.
    api_url = f"https://api.tumblr.com/v2/blog/{blog}/posts"
    params = {'api_key': TUMBLR_API_KEY, 'reblog_info': 'true', 'npf': 'true', 'limit': 20, **extra_params}
    found, offset = [], 0
    while True:
        response = requests.get(api_url, params={**params, 'offset': offset}, timeout=30)
        response.raise_for_status()
        posts = response.json().get('response', {}).get('posts', [])
        if not posts: break

        # with no tags to track, only posts that have at least one tag are archived
        wanted = [p for p in posts if p.get('tags')] if require_tags else posts
        found.extend(wanted)
        if not BACKFILL and all(post_id_of(p) in known_ids for p in wanted): break
        offset += len(posts)
    return found

def fetch_post_by_id(blog, post_id):
    response = requests.get(f"https://api.tumblr.com/v2/blog/{blog}/posts",
                            params={'api_key': TUMBLR_API_KEY, 'reblog_info': 'true', 'npf': 'true', 'id': post_id}, timeout=30)
    if response.status_code == 404: return None
    response.raise_for_status()
    posts = response.json().get('response', {}).get('posts', [])
    return posts[0] if posts else None

def cross_archive(post):
    # a reblog tagged with one of its blog's crossArchiveTags from blogs.json
    # also files the older blog's post straight into that blog's own archive. the reblog itself is archived as normal.
    # the chain is cut at the last entry by that blog: if that's the op it becomes a plain original post,
    # and if it reblogged someone else it stays a reblog by that blog. the new blog's reblog and tags are left out.
    own = tumblr_name(post.get('blog_name'))
    own_tags = {tag.lower() for blog in MY_BLOGS if blog['tumblr'] == own for tag in blog.get('crossArchiveTags', [])}
    wanted = None
    for tag in post.get('tags', []):
        match = re.match(r'^archive:\s*(.+)$', tag.strip(), re.I)
        if match:
            wanted = match.group(1).strip().lower()
            break
        if tag.strip().lower() in own_tags:
            wanted = 'any'
    if not wanted: return

    trail = post.get('trail') or []
    def owned_by(names):
        return [i for i, block in enumerate(trail) if tumblr_name(block_blog_name(block)) in names]

    if wanted == 'any':
        # another of my blogs comes first. failing that, it's this blog reblogging its own older post
        candidates = [blog for blog in MY_BLOGS if blog['tumblr'] != own]
        owned = owned_by({blog['tumblr'] for blog in candidates})
        if not owned:
            candidates = [blog for blog in MY_BLOGS if blog['tumblr'] == own]
            owned = owned_by({own})
    else:
        candidates = [blog for blog in MY_BLOGS if wanted in (blog['id'].lower(), blog['tumblr'])]
        owned = owned_by({blog['tumblr'] for blog in candidates})
    if not owned:
        print(f"  -> post {post_id_of(post)} is tagged for archiving, but none of listed other blogs' posts are in the reblog chain. skipping.")
        return

    index = owned[-1]
    entry, kept = trail[index], trail[:index]
    entry_id = str((entry.get('post') or {}).get('id') or '')
    if not entry_id: return
    owner = tumblr_name(block_blog_name(entry))
    candidates = [blog for blog in candidates if blog['tumblr'] == owner]
    # "archive: wildwizardcreation-2" names its folder outright, otherwise the closest id range decides
    exact = [blog for blog in candidates if blog['id'].lower() == wanted]
    folder = (exact or [closest_blog_by_id(candidates, int(entry_id))])[0]['id']

    copy = {'id': entry_id, 'id_string': entry_id, 'type': post.get('type'), 'blog_name': owner,
            'content': entry.get('content', []), 'layout': entry.get('layout', []), 'trail': kept,
            'timestamp': (entry.get('post') or {}).get('timestamp'), 'tags': [],
            '_source': {'type': 'cross-archive', 'reblog': f"{post.get('blog_name')}/{post_id_of(post)}"}}
    if kept:
        copy['reblogged_from_name'] = block_blog_name(kept[-1])
        copy['reblogged_root_name'] = block_blog_name(kept[0])
    save_raw_post(copy, os.path.join(BASE_OUTPUT_DIR, folder, "posts"))
    POST_INDEX[entry_id] = folder

def archive_blog(blog):
    # fetches a live blog's new posts and saves their raw data.
    print(f"\n--- archiving '{blog}' ---")
    posts_dir = os.path.join(BASE_OUTPUT_DIR, blog, "posts")
    known_ids = ids_with(posts_dir, '.json')
    print(f"found {len(known_ids)} posts with raw data already saved for this blog.")

    # a post with two tracked tags comes back once per tag, so everything is collected by id first
    fetched = {}
    try:
        if TAGS_TO_ARCHIVE:
            for tag in TAGS_TO_ARCHIVE:
                print(f"--- processing tag: '{tag}' ---")
                for post in fetch_posts(blog, {'tag': tag}, known_ids):
                    fetched[post_id_of(post)] = post
        else:
            # if no tags are specified, only process posts that have at least one tag.
            print("--- processing latest posts (tagged posts only) ---")
            for post in fetch_posts(blog, {}, known_ids, require_tags=True):
                fetched[post_id_of(post)] = post

        if BACKFILL:
            # posts that were archived before raw json existed get theirs fetched one by one
            missing = sorted(ids_with(posts_dir, '.html') - known_ids - set(fetched))
            print(f"--- backfill: fetching raw data for {len(missing)} older posts ---")
            for post_id in missing:
                post = fetch_post_by_id(blog, post_id)
                if post: fetched[post_id] = post
                else: print(f"  -> post {post_id} is gone from tumblr, keeping its html as it is")
    except requests.exceptions.RequestException as e:
        print(f"!!! api request failed: {e}")
    except Exception as e:
        print(f"!!! an unexpected error occurred: {e}")

    new_count = len(set(fetched) - known_ids)
    print(f"fetched {len(fetched)} posts, {new_count} of them new.")
    for post in fetched.values():
        # one bad post shouldn't stop the rest
        try:
            save_raw_post(post, posts_dir)
            cross_archive(post)
        except Exception as e: print(f"!!! couldn't save post {post.get('id')}: {e}")

# --- polls ---

def refresh_polls(blog):
    # poll results are fetched on every run while a poll is open, and one last time after it closes.
    # they're saved inside the post's raw json, so the final numbers stick around even if the poll disappears later.
    posts_dir = os.path.join(BASE_OUTPUT_DIR, blog, "posts")
    for post_id in sorted(ids_with(posts_dir, '.json')):
        path = os.path.join(posts_dir, f"{post_id}.json")
        post = read_json(path, None)
        if not post: continue

        changed = False
        try:
            for block in post_chain(post):
                poll_blog = block_blog_name(block)
                poll_post_id = post_id if block is post else str((block.get('post') or {}).get('id') or '')
                for poll in (b for b in block.get('content', []) if b.get('type') == 'poll' and b.get('client_id')):
                    saved = post.setdefault('_polls', {}).get(poll['client_id'])
                    # polls run for up to a week. the block says when it was made and how long it stays open.
                    closes_at = (poll.get('timestamp') or post.get('timestamp') or 0) + (poll.get('settings') or {}).get('expire_after', 7 * 86400)
                    if saved and saved.get('checked', 0) >= closes_at: continue
                    if not poll_blog or not poll_post_id: continue

                    saved = saved or {}
                    try:
                        result_url = f"https://www.tumblr.com/api/v2/polls/{poll_blog}/{poll_post_id}/{poll['client_id']}/results"
                        response = requests.get(result_url, params={'api_key': TUMBLR_API_KEY}, timeout=30)
                        response.raise_for_status()
                        # the response.results is the map of {answer_client_id: votes}
                        saved['results'] = response.json().get('response', {}).get('results', {})
                    except Exception as e:
                        print(f"  -> couldn't fetch poll results for post {post_id}: {e}")
                    saved['checked'] = int(time.time())
                    post['_polls'][poll['client_id']] = saved
                    changed = True
        except Exception as e:
            # one bad post shouldn't stop the rest
            print(f"!!! couldn't check polls for post {post_id}: {e}")

        if not post.get('_polls'): post.pop('_polls', None)
        if changed: write_json(path, post)

# --- media mirroring ---

WAYBACK_PREFIX = re.compile(r'^(?:https?:)?(?://web\.archive\.org)?/web/\d+[a-z_]*/', re.I)
IMG_TAG = re.compile(r'<img\b[^>]*>')
MEDIA_TAG = re.compile(r'<(img|video|audio)\b[^>]*>')

def clean_wayback_url(url):
    # wayback wraps media urls and links to point at its own copy (https://web.archive.org/web/<timestamp>/https://...).
    # this strips that so only the original url is left. only one layer comes off, so a link that really was to wayback stays one.
    url = (url or '').strip()
    cleaned = WAYBACK_PREFIX.sub('', url, count=1)
    if cleaned != url:
        cleaned = re.sub(r'^(https?):/(?!/)', r'\1://', cleaned, flags=re.I)
        if cleaned.startswith('//'): cleaned = 'https:' + cleaned
        elif not re.match(r'^[a-z][a-z0-9+.-]*:(?!\d)', cleaned, re.I): cleaned = 'https://' + cleaned
    return cleaned

def is_tumblr_media(url):
    return (urlparse(url).netloc or '').lower().endswith('media.tumblr.com')

def is_tumblr_audio(url):
    # tumblr's own audio uploads (usually mp3s) live on a.tumblr.com rather than the media servers
    host = (urlparse(url).netloc or '').lower()
    return host == 'a.tumblr.com' or (host.endswith('media.tumblr.com') and bool(re.search(r'\.(mp3|m4a|ogg|wav)$', urlparse(url).path, re.I)))

def audio_source(block):
    # the file behind a tumblr-hosted audio block, or '' for spotify, soundcloud and the like
    media = block.get('media')
    if isinstance(media, list): media = media[0] if media else None
    url = (media or {}).get('url') or block.get('url') or ''
    hosted = block.get('provider') == 'tumblr' or str((media or {}).get('type', '')).startswith('audio/')
    return url if hosted and is_tumblr_audio(url) else ''

AUDIO_TAG = re.compile(r'<audio\b[^>]*>')

def blog_media_urls(blog):
    # every image and tumblr-hosted audio file in a blog's archive, paired with the post it belongs to.
    # posts with raw json are read from that, and posts that only exist as html get their <img> and <audio> tags read instead.
    # video is left out, since it would eat through the bucket's free storage. (exported videos still get there by hand.)
    posts_dir = os.path.join(BASE_OUTPUT_DIR, blog, "posts")
    urls = {}
    json_ids = ids_with(posts_dir, '.json')
    for post_id in sorted(json_ids):
        post = read_json(os.path.join(posts_dir, f"{post_id}.json"), {})
        try:
            for block in post_chain(post):
                for content in block.get('content', []):
                    if content.get('type') == 'image' and content.get('media'):
                        urls.setdefault(content['media'][0]['url'], post_id)
                    elif content.get('type') == 'audio' and audio_source(content):
                        urls.setdefault(audio_source(content), post_id)
        except Exception as e:
            print(f"!!! couldn't read the images of post {post_id}: {e}")
    for post_id in sorted(ids_with(posts_dir, '.html') - json_ids):
        with open(os.path.join(posts_dir, f"{post_id}.html"), encoding='utf-8') as f: page = f.read()
        for tag in IMG_TAG.findall(page):
            source = re.search(r'\bdata-tumblr-src="([^"]*)"', tag) or re.search(r'\bsrc="([^"]*)"', tag)
            url = clean_wayback_url(html.unescape(source.group(1))) if source else ''
            if is_tumblr_media(url): urls.setdefault(url, post_id)
        for tag in AUDIO_TAG.findall(page):
            source = re.search(r'\bdata-tumblr-src="([^"]*)"', tag) or re.search(r'\ssrc="([^"]*)"', tag)
            url = clean_wayback_url(html.unescape(source.group(1))) if source else ''
            if is_tumblr_audio(url): urls.setdefault(url, post_id)
    return urls

def mirror_media(blog):
    # copies every image and audio file that isn't mirrored yet into the bucket and records it in the blog's media.json.
    # the bucket is only a backup. mirroring never blocks archiving, and failed downloads are retried next run.
    media_path = os.path.join(BASE_OUTPUT_DIR, blog, "media.json")
    media = read_json(media_path, {})
    pending = {url: post_id for url, post_id in blog_media_urls(blog).items() if (media.get(url) or {}).get('status') != 'ok'}
    if not pending: return media
    if not (B2_KEY_ID and B2_APP_KEY and B2_BUCKET and B2_ENDPOINT):
        print(f"  -> {len(pending)} files in '{blog}' aren't mirrored yet, but the bucket isn't configured. skipping.")
        return media

    import boto3
    endpoint = B2_ENDPOINT if B2_ENDPOINT.startswith('http') else f"https://{B2_ENDPOINT}"
    bucket = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=B2_KEY_ID, aws_secret_access_key=B2_APP_KEY)

    print(f"  -> mirroring {len(pending)} files for '{blog}'")
    for url, post_id in pending.items():
        # the key reuses tumblr's own filename, so a file can always be matched back to its url by eye too
        entry = {'key': f"{blog}/{post_id}/{media_filename(url)}", 'post': post_id}
        try:
            audio = is_tumblr_audio(url)
            response = requests.get(url, headers={'Accept': 'audio/*' if audio else 'image/*'}, timeout=120 if audio else 60)
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
            if audio:
                # tumblr sometimes serves its audio as plain binary. a web page means the file is gone.
                if content_type.startswith('text/'): raise ValueError(f"got {content_type} instead of an audio file")
                if not content_type.startswith('audio/'): content_type = mimetypes.guess_type(urlparse(url).path)[0] or 'audio/mpeg'
            elif not content_type.startswith('image/'):
                raise ValueError(f"got {content_type or 'no content type'} instead of an image")
            bucket.put_object(Bucket=B2_BUCKET, Key=entry['key'], Body=response.content, ContentType=content_type)
            entry.update(bytes=len(response.content), sha256=hashlib.sha256(response.content).hexdigest(), status='ok',
                         mirrored=datetime.now(timezone.utc).strftime('%Y-%m-%d'))
        except Exception as e:
            print(f"  -> couldn't mirror {url}: {e}")
            entry['status'] = 'failed'
        media[url] = entry

    write_json(media_path, dict(sorted(media.items())))
    return media

def refresh_exported_media(match):
    # one <img>, <video> or <audio> in a post that only exists as html. mirrored files get the same attributes
    # the builder writes, for the src and (on videos) the poster.
    tag, name = match.group(0), match.group(1)
    for attr in ('src', 'poster'):
        suffix = '' if attr == 'src' else f'-{attr}'
        source = re.search(rf'\bdata-tumblr-{attr}="([^"]*)"', tag) or re.search(rf'\s{attr}="([^"]*)"', tag)
        url = clean_wayback_url(html.unescape(source.group(1))) if source else ''
        if not url or (MEDIA.get(url) or {}).get('status') != 'ok': continue
        tag = re.sub(rf'\s+(?:{attr}|data-mirror{suffix}|data-tumblr-{attr})="[^"]*"', '', tag)
        tag = tag.replace(f'<{name}', f'<{name} {media_attributes(url, attr)}', 1)
    return tag

# --- rebuilding ---

def rebuild_blog(blog):
    # rebuilds the html of every post that has raw json, then writes the blog's posts.json for the frontend.
    posts_dir = os.path.join(BASE_OUTPUT_DIR, blog, "posts")
    json_ids, html_ids = ids_with(posts_dir, '.json'), ids_with(posts_dir, '.html')
    listing, rebuilt = [], 0

    for post_id in sorted(json_ids | html_ids, key=int, reverse=True):
        html_path = os.path.join(posts_dir, f"{post_id}.html")
        try:
            with open(html_path, encoding='utf-8') as f: existing = f.read()
        except FileNotFoundError:
            existing = ''
        # posts i've edited by hand are marked with archive-special-edit and never get rebuilt over
        hand_edited = 'archive-special-edit' in existing

        if post_id in json_ids and not hand_edited:
            try:
                post = read_json(os.path.join(posts_dir, f"{post_id}.json"), None)
                page = render_post(post)
                if write_if_changed(html_path, page): rebuilt += 1
                listing.append({'id': post_id, 'timestamp': post.get('timestamp'), 'type': post_type(post), 'tags': post.get('tags', []),
                                'reblogged_from': post.get('reblogged_from_name') or None,
                                'source': (post_source(post) or {}).get('type')})
                continue
            except Exception as e:
                # one bad post shouldn't stop the rest. whatever html it already has stays.
                print(f"!!! couldn't rebuild post {post_id}: {e}")
                if not existing: continue

        # no raw json to rebuild from (or hand edited), so the html stays as it is apart from its media mirrors and links,
        # and the listing is read out of the page itself
        page = existing if hand_edited else ANCHOR_TAG.sub(refresh_exported_link, MEDIA_TAG.sub(refresh_exported_media, existing))
        if page != existing and write_if_changed(html_path, page): rebuilt += 1
        main_tag = re.search(r'<main\b[^>]*>', page)
        main_tag = main_tag.group(0) if main_tag else ''
        attribute = lambda name: (re.search(rf'\b{name}="([^"]*)"', main_tag) or [None, None])[1]
        listing.append({'id': post_id, 'timestamp': None,
                        'type': attribute('data-type') or ('reblog' if 'class="reblog-header"' in page else 'original'),
                        'tags': [html.unescape(tag) for tag in re.findall(r'class="tag">#([^<]*)</a>', page)],
                        'reblogged_from': html.unescape(attribute('data-reblogged-from') or '') or None,
                        'source': attribute('data-source')})

    if write_json(os.path.join(BASE_OUTPUT_DIR, blog, "posts.json"), listing) or rebuilt:
        print(f"  -> '{blog}': {len(listing)} posts listed, {rebuilt} html files updated")

def index_archive():
    # notes which folder every archived post is filed under, and each folder's id range
    POST_INDEX.clear(); ID_RANGES.clear()
    if not os.path.isdir(BASE_OUTPUT_DIR): return []
    folders = sorted(name for name in os.listdir(BASE_OUTPUT_DIR) if os.path.isdir(os.path.join(BASE_OUTPUT_DIR, name, "posts")))
    for folder in folders:
        posts_dir = os.path.join(BASE_OUTPUT_DIR, folder, "posts")
        ids = ids_with(posts_dir, '.json') | ids_with(posts_dir, '.html')
        for post_id in ids: POST_INDEX[post_id] = folder
        if ids: ID_RANGES[folder] = (min(map(int, ids)), max(map(int, ids)))
    return folders

if __name__ == "__main__":
    MY_BLOGS.extend(load_blogs())

    # every blog in blogs.json that's still going gets archived, plus BLOG_IDENTIFIER if it's set
    live_blogs = list(dict.fromkeys([blog['id'] for blog in MY_BLOGS if blog['live']] + ([BLOG_IDENTIFIER] if BLOG_IDENTIFIER else [])))

    if live_blogs and not TUMBLR_API_KEY:
        print("!!! configuration incomplete: set the TUMBLR_API_KEY environment variable.")
        exit(1)
    if not live_blogs:
        print("!!! configuration incomplete: no live blog in blogs.json, and BLOG_IDENTIFIER isn't set.")
        exit(1)

    print(f"--- starting github archive for: {', '.join(live_blogs)}{' (backfill)' if BACKFILL else ''} ---")
    index_archive()
    for blog in live_blogs:
        archive_blog(blog)
        refresh_polls(blog)

    # every folder gets rebuilt, including the old blogs that only hold hand-filed posts
    print("\n--- rebuilding html ---")
    for blog in index_archive():
        MEDIA.clear()
        try:
            MEDIA.update(mirror_media(blog))
            rebuild_blog(blog)
        except Exception as e:
            print(f"!!! an unexpected error occurred while rebuilding '{blog}': {e}")

    print("\ngitHub archiving process complete.")
