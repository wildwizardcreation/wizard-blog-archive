# script for archiving tumblr posts, designed to run in a github actions workflow.
# mostly uses the NPF (new post format) content parser for ease of api access
# legacy fallbacks for all content types in the works

import os
import requests
import html
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

# --- configuration ---
load_dotenv()

# these should be set as environment secrets/variables in the github repository.
TUMBLR_API_KEY = os.environ.get("TUMBLR_API_KEY")
BLOG_IDENTIFIER = os.environ.get("BLOG_IDENTIFIER")
TAGS_STRING = os.environ.get("TAGS_TO_ARCHIVE", "")
TAGS_TO_ARCHIVE = [tag.strip() for tag in TAGS_STRING.split(',') if tag.strip()]

# the base directory where all blog archives will be stored.
BASE_OUTPUT_DIR = "blogs"

# --- html template ---
# defines the structure for each archived post's html file.
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="stylesheet" href="../style.css">
</head>
<body>
    <div class="container">
        <main class="archive-post">
            {content}
        </main>
        <footer class="post-meta">
            {metadata}
        </footer>
    </div>
</body>
</html>
"""

# --- formatting helper functions ---

def format_datetime_for_display(dt_obj):
    # formats a datetime object into a more readable 'month day, year · hour:minute am/pm' string.
    if not dt_obj:
        return "???"
    try:
        formatted_time = dt_obj.strftime("%I:%M %p")
        if formatted_time.startswith('0'):
            formatted_time = formatted_time[1:]
        return dt_obj.strftime(f"%B %d, %Y · {formatted_time}")
    except (ValueError, TypeError):
        return "???"

def format_op_header(username, date_str):
    # formats the header for the original post
    return f'<div class="op-block">\n  <p class="user-info">{html.escape(username, quote=False)}</p>\n  <p class="posted">Posted · <span>{date_str}</span></p>\n</div>'

def format_op_content(op_html):
    # formats the container for the main op content
    return f'<div class="op-content">\n{op_html.strip()}\n</div>\n'

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

# --- layout-aware content parser ---
# this section reconstructs post layouts, including image galleries and "read more" breaks as accurately as possible

def _process_single_block(block, active_list_type_ref, blog_name=None, post_id=None):
    # helper to process one content block. modifies active_list_type_ref for list tracking.
    # blog_name and post_id are needed to fetch live poll results.
    block_html, media = '', []
    active_list_type = active_list_type_ref[0]
    block_type, subtype = block.get('type'), block.get('subtype')

    is_list_item = subtype in ('ordered-list-item', 'unordered-list-item')
    if active_list_type and not is_list_item:
        block_html += f'</{active_list_type}>\n'
        active_list_type = None

    if block_type == 'text':
        raw_text, formatting = block.get('text', ''), block.get('formatting', [])
        markers = defaultdict(list)
        for fmt in formatting:
            start, end, fmt_type = fmt['start'], fmt['end'], fmt['type']
            # be strict with escaping href attributes
            tags = {'bold': ('<strong>', '</strong>'), 'italic': ('<em>', '</em>'), 'small': ('<small>', '</small>'),
                    'strikethrough': ('<s>', '</s>'), 'link': (f'<a href="{html.escape(fmt.get("url", "#"), quote=True)}">', '</a>')}
            if fmt_type in tags:
                markers[start].append(tags[fmt_type][0])
                markers[end].insert(0, tags[fmt_type][1])
        
        text_parts, last_index = [], 0
        for index in sorted(markers.keys()):
            text_parts.append(html.escape(raw_text[last_index:index], quote=False))
            text_parts.extend(markers[index])
            last_index = index
        text_parts.append(html.escape(raw_text[last_index:], quote=False))
        
        # join all parts and perform tumblr-esque arrow replacements
        text = "".join(text_parts)
        text = text.replace("&lt;-", "\U0001F850").replace("-&gt;", "\U0001F852")
        
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
        alt_text = block.get('alt_text', 'Tumblr Image')
        media.append(url)
        block_html += f'<img src="{url}" alt="{html.escape(alt_text, quote=False)}">\n'

    elif block_type == 'poll':
        print("--- debug: found poll block ---")
        question = block.get('question', 'Poll')
        display_answers = block.get('answers', [])
        
        # fetch poll results from the dedicated endpoint
        votes_map = {}
        total_votes = 0
        poll_client_id = block.get('client_id')
        
        if blog_name and post_id and poll_client_id and TUMBLR_API_KEY:
            print(f"--- debug: fetching live results for poll {poll_client_id} ---")
            try:
                # lots of console checks because polls are super finicky
                result_url = f"https://www.tumblr.com/api/v2/polls/{blog_name}/{post_id}/{poll_client_id}/results?api_key={TUMBLR_API_KEY}"
                response = requests.get(result_url)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('meta', {}).get('status') == 200:
                        # the response.results is the map of {answer_client_id: votes}
                        votes_map = data.get('response', {}).get('results', {})
                        total_votes = sum(votes_map.values())
                        print(f"--- debug: successfully fetched poll results. total votes: {total_votes} ---")
                    else:
                        print(f"--- debug: poll result api meta-status was not 200: {data.get('meta', {}).get('msg')} ---")
                else:
                    print(f"--- debug: api call for poll results failed with status {response.status_code} ---")
            except Exception as e:
                print(f"--- debug: exception during poll api call: {e} ---")
        else:
            print(f"--- debug: missing blog_name ({blog_name}), post_id ({post_id}), poll_client_id ({poll_client_id}), or api key. skipping live poll results. ---")

        block_html += '<div class="poll-block">\n'
        poll_question_text = html.escape(question, quote=False).replace("&lt;-", "\U0001F850").replace("-&gt;", "\U0001F852")
        block_html += f'  <p class="poll-question"><strong>{poll_question_text}</strong></p>\n'
        
        if display_answers:
            block_html += '  <ul class="poll-options">\n'
            for answer in display_answers:
                answer_text = answer.get('answer_text', '...')
                client_id = answer.get('client_id')
                votes = votes_map.get(client_id, 0)
                print(f"--- debug: processing answer '{answer_text[:20]}...': client_id={client_id}, found votes={votes} ---")
                
                percentage = 0.0
                if total_votes > 0:
                    percentage = (votes / total_votes) * 100
                percentage_str = f' <span class="poll-percentage">({percentage:.0f}%)</span>'
                    
                poll_answer_text = html.escape(answer_text, quote=False).replace("&lt;-", "\U0001F850").replace("-&gt;", "\U0001F852")
                block_html += (
                    f'    <li style="background: linear-gradient(to right, #f0f0f0 {percentage:.0f}%, transparent {percentage:.0f}%); '
                    f'padding: 0.25em 0.5em;" data-percentage="{percentage:.0f}">'
                    f'{poll_answer_text}{percentage_str}'
                    f'</li>\n'
                )
            block_html += '  </ul>\n'
        if total_votes > 0:
            block_html += f'  <p class="poll-total-votes">{total_votes:,} votes total</p>\n'
        block_html += '</div>\n'

    active_list_type_ref[0] = active_list_type
    return {'html': block_html, 'media': media}

def parse_api_content(all_content_blocks, layout_blocks=None, indices_to_process=None, blog_name=None, post_id=None):
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
                     result = _process_single_block(all_content_blocks[index], active_list_type_ref, blog_name=blog_name, post_id=post_id)
                     current_row_html += result['html']; media.extend(result['media'])
                current_row_html += '</div>\n'
            else:
                result = _process_single_block(all_content_blocks[relevant_indices[0]], active_list_type_ref, blog_name=blog_name, post_id=post_id)
                current_row_html += result['html']; media.extend(result['media'])
            
            if min(relevant_indices) > last_visible_idx: html_after += current_row_html
            else: html_before += current_row_html
    else:
        for index in indices_to_process:
            result = _process_single_block(all_content_blocks[index], active_list_type_ref, blog_name=blog_name, post_id=post_id)
            html_before += result['html']; media.extend(result['media'])

    if active_list_type_ref[0]:
        closing_tag = f'</{active_list_type_ref[0]}>\n'
        if html_after: html_after = html_after.rstrip('\n') + closing_tag
        else: html_before = html_before.rstrip('\n') + closing_tag
            
    final_html = html_before
    if html_after:
        final_html += f'<details>\n  <summary>Keep Reading</summary>\n{html_after}</details>\n'
    
    return {'html': final_html, 'media': list(dict.fromkeys(media))}

# archiving logic

def get_all_archived_ids(directory):
    # scans the output directory for already archived post ids to prevent duplicates.
    archived_ids = set()
    if not os.path.exists(directory):
        return archived_ids
    
    for file in os.listdir(directory):
        if file.endswith('.html'):
            archived_ids.add(file.split('.')[0])
    return archived_ids


def save_post_to_file(post, output_dir):
    # processes a single post dictionary and saves it as a self-contained html file.
    post_id = str(post['id'])
    print(f"  -> processing post id: {post_id}")

    html_body = ""
    has_trail = bool(post.get('trail'))
    
    is_answer = post.get('type') == 'answer' or post.get('question')
    if not is_answer:
        layout_to_check = post.get('layout', []) if not has_trail else post.get('trail', [{}])[0].get('layout', [])
        if any(block.get('type') == 'ask' for block in layout_to_check):
            is_answer = True
            print("  -> ask detection: found npf 'ask' layout block.")

    full_chain = (post.get('trail', []) + [post]) if has_trail else [post]
    
    for i, block in enumerate(full_chain):
        is_root = i == 0
        username = block.get('blog_name') or block.get('blog', {}).get('name')
        content_to_parse, layout_to_use = block.get('content', []), block.get('layout', [])
        
        if not username or (not is_root and not content_to_parse): continue

        # determine the post id for the *current* block in the chain
        is_final_reblogger = i == len(full_chain) - 1
        current_post_id = str(post['id']) if is_final_reblogger else str(block.get('post', {}).get('id'))
        if not current_post_id:
             # failsafe, though shouldn't happen if block has content
             current_post_id = post_id 
             print(f"--- debug: could not find post id for trail block {i}, falling back to root id {post_id} ---")

        if is_root:
            try: dt_obj = datetime.fromtimestamp(post.get('reblogged_root_timestamp', post.get('timestamp')))
            except (ValueError, TypeError): dt_obj = None
            html_body += format_op_header(username, format_datetime_for_display(dt_obj))

            if is_answer:
                ask_block = next((b for b in layout_to_use if b.get('type') == 'ask'), None)
                if ask_block:
                    asker = "Anonymous"
                    if 'attribution' in ask_block and isinstance(ask_block.get('attribution'), dict) and ask_block['attribution'].get('type') == 'blog':
                        asker = ask_block['attribution']['blog']['name']
                    ask_indices = ask_block.get('blocks', [])
                    answer_indices = [idx for idx in range(len(content_to_parse)) if idx not in ask_indices]
                    parsed_ask = parse_api_content(content_to_parse, layout_to_use, ask_indices, blog_name=username, post_id=current_post_id)
                    parsed_answer = parse_api_content(content_to_parse, layout_to_use, answer_indices, blog_name=username, post_id=current_post_id)
                    html_body += format_ask_block(asker, parsed_ask['html'])
                    html_body += format_answer_block(username, parsed_answer['html'], include_header=has_trail)
                else: # legacy ask fallback
                    parsed_content = parse_api_content(content_to_parse, layout_to_use, blog_name=username, post_id=current_post_id)
                    question_text = html.escape(post.get('question', ''), quote=False)
                    question_text = question_text.replace("&lt;-", "\U0001F850").replace("-&gt;", "\U0001F852")
                    html_body += format_ask_block(post.get('asking_name', 'Anonymous'), f"<blockquote>{question_text}</blockquote>")
                    html_body += format_answer_block(username, parsed_content['html'], include_header=has_trail)
            else:
                parsed_content = parse_api_content(content_to_parse, layout_to_use, blog_name=username, post_id=current_post_id)
                html_body += format_op_content(parsed_content['html'])
        else:
            parsed_content = parse_api_content(content_to_parse, layout_to_use, blog_name=username, post_id=current_post_id)
            
            # don't create an empty block for the final reblogger if they only added tags
            if is_final_reblogger and not parsed_content['html'].strip(): continue
            
            try: 
                 dt_str = block.get('date', '') if is_final_reblogger else block.get('post', {}).get('date', '')
                 dt_obj = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S %Z') if dt_str else None
            except (ValueError, TypeError): dt_obj = None

            html_body += format_reblog_block(username, format_datetime_for_display(dt_obj), parsed_content['html'])

    metadata = {'id': post_id, 'blog': post.get('blog_name', 'N/A'), 'date_archived': datetime.now(), 'tags': post.get('tags', [])}
    metadata_html = f"<p><strong>{metadata['blog']}</strong> | post id <strong>{metadata['id']}</strong></p>"
    metadata_html += f"<p><strong>archived:</strong> {format_datetime_for_display(metadata['date_archived'])}</p>"
    if metadata['tags']:
        tags_html = "".join([f'<a href="#" class="tag">#{tag}</a>' for tag in metadata['tags']])
        metadata_html += f'<div class="tags-container">{tags_html}</div>'

    final_html = HTML_TEMPLATE.format(title=f"{metadata['blog']} - post {metadata['id']}", metadata=metadata_html, content=html_body.strip())
    
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, f"{post_id}.html"), 'w', encoding='utf-8') as f: f.write(final_html)
    print(f"  -> successfully archived post to {os.path.join(output_dir, f'{post_id}.html')}")

def fetch_and_process(api_url, params, output_dir, existing_ids, require_tags=False):
    # generic function to fetch posts from the api and process them.
    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        posts = response.json().get('response', {}).get('posts', [])
        
        if not posts:
            print("no posts found for the given criteria.")
            return

        new_posts = [p for p in posts if str(p['id']) not in existing_ids]
        
        if not new_posts:
            print(f"found {len(posts)} posts, but all are already archived. nothing to do.")
            return
            
        # filter for posts that have tags, if required
        if require_tags:
            tagged_new_posts = [p for p in new_posts if p.get('tags')] # checks for non-empty list
            print(f"found {len(posts)} total posts, {len(new_posts)} are new. {len(tagged_new_posts)} of them have tags.")
            new_posts = tagged_new_posts
        else:
            print(f"found {len(posts)} total posts, {len(new_posts)} are new. processing...")

        if not new_posts:
            print("no new posts matching all criteria found. nothing to do.")
            return
            
        for post in new_posts:
            save_post_to_file(post, output_dir)
            
    except requests.exceptions.RequestException as e:
        print(f"!!! api request failed: {e}")
    except Exception as e:
        print(f"!!! an unexpected error occurred: {e}")

if __name__ == "__main__":
    if not TUMBLR_API_KEY or not BLOG_IDENTIFIER:
        print("!!! configuration incomplete: set TUMBLR_API_KEY and BLOG_IDENTIFIER environment variables.")
        exit(1)

    print(f"--- starting github archive for blog '{BLOG_IDENTIFIER}' ---")
    
    # create a specific directory for the blog being archived.
    blog_output_dir = os.path.join(BASE_OUTPUT_DIR, BLOG_IDENTIFIER, "posts")
    
    archived_ids = get_all_archived_ids(blog_output_dir)
    print(f"found {len(archived_ids)} posts already archived for this blog.")
    
    api_url = f"https://api.tumblr.com/v2/blog/{BLOG_IDENTIFIER}/posts"
    params = {'api_key': TUMBLR_API_KEY, 'reblog_info': 'true', 'npf': 'true'}

    if TAGS_TO_ARCHIVE:
        for tag in TAGS_TO_ARCHIVE:
            print(f"\n--- processing tag: '{tag}' ---")
            params['tag'] = tag
            fetch_and_process(api_url, params, blog_output_dir, existing_ids=archived_ids)
    else:
        # if no tags are specified, fetch the latest 25 posts. 
        # only process posts that have at least one tag.
        print(f"\n--- processing latest 25 posts for blog '{BLOG_IDENTIFIER}' (tagged posts only) ---")
        params['limit'] = 25 # type: ignore
        fetch_and_process(api_url, params, blog_output_dir, existing_ids=archived_ids, require_tags=True)

    print("\ngitHub archiving process complete.")