from io import BytesIO
import html
import random
import requests
import modules.scripts as scripts
import gradio as gr
import os
from PIL import Image
import numpy as np
import importlib
import requests_cache
import json

from modules.processing import process_images, StableDiffusionProcessingImg2Img
from modules import shared
try:
    from modules.sd_hijack import model_hijack
except ImportError:
    model_hijack = None
try:
    from modules import deepbooru
except ImportError:
    deepbooru = None
from modules.ui_components import InputAccordion

extension_root = scripts.basedir()
user_data_dir = os.path.join(extension_root, 'user')
user_search_dir = os.path.join(user_data_dir, 'search')
user_remove_dir = os.path.join(user_data_dir, 'remove')
user_credentials_dir = os.path.join(user_data_dir, 'credentials')
os.makedirs(user_search_dir, exist_ok=True)
os.makedirs(user_remove_dir, exist_ok=True)
os.makedirs(user_credentials_dir, exist_ok=True)

# Initialize credentials manager
class CredentialsManager:
    def __init__(self, extension_root):
        self.extension_root = extension_root
        self.credentials_dir = os.path.join(extension_root, 'user', 'credentials')
        self.credentials_file = os.path.join(self.credentials_dir, 'credentials.json')
        
        # Create credentials directory if it doesn't exist
        os.makedirs(self.credentials_dir, exist_ok=True)
        
        # Initialize credentials file if it doesn't exist
        if not os.path.exists(self.credentials_file):
            self._save_credentials({})
    
    def _load_credentials(self):
        """Load credentials from the JSON file"""
        try:
            with open(self.credentials_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
    
    def _save_credentials(self, credentials):
        """Save credentials to the JSON file"""
        with open(self.credentials_file, 'w') as f:
            json.dump(credentials, f, indent=2)
    
    def save_booru_credentials(self, booru_name, api_key, user_id=None):
        """Save API credentials for a specific booru"""
        credentials = self._load_credentials()
        
        if booru_name not in credentials:
            credentials[booru_name] = {}
        
        credentials[booru_name]['api_key'] = api_key
        if user_id is not None:
            credentials[booru_name]['user_id'] = user_id
        
        self._save_credentials(credentials)
    
    def get_booru_credentials(self, booru_name):
        """Get API credentials for a specific booru"""
        credentials = self._load_credentials()
        return credentials.get(booru_name, {})
    
    def has_credentials(self, booru_name):
        """Check if credentials exist for a specific booru"""
        credentials = self.get_booru_credentials(booru_name)
        return 'api_key' in credentials and credentials['api_key'].strip() != ''
    
    def clear_booru_credentials(self, booru_name):
        """Clear credentials for a specific booru"""
        credentials = self._load_credentials()
        if booru_name in credentials:
            del credentials[booru_name]
            self._save_credentials(credentials)

credentials_manager = CredentialsManager(extension_root)

if not os.path.isfile(os.path.join(user_search_dir, 'tags_search.txt')):
    with open(os.path.join(user_search_dir, 'tags_search.txt'), 'w'):
        pass
if not os.path.isfile(os.path.join(user_remove_dir, 'tags_remove.txt')):
    with open(os.path.join(user_remove_dir, 'tags_remove.txt'), 'w'):
        pass

COLORED_BG = ['black_background', 'aqua_background', 'white_background', 'colored_background', 'gray_background', 'blue_background', 'green_background', 'red_background', 'brown_background', 'purple_background', 'yellow_background', 'orange_background', 'pink_background', 'plain', 'transparent_background', 'simple_background', 'two-tone_background', 'grey_background']
ADD_BG = ['outdoors', 'indoors']
BW_BG = ['monochrome', 'greyscale', 'grayscale']
POST_AMOUNT = 100
COUNT = 100 #Number of images the search returned. Booru classes below were modified to update this value with the latest search result count.
DEBUG = False
REQUEST_TIMEOUT = 20  # seconds; prevents a stalled booru from hanging generation
DEFAULT_BOORU = 'safebooru'  # single source of truth for the default booru

# Booru requests go through a dedicated CachedSession rather than
# requests_cache.install_cache(), which would monkeypatch `requests` globally
# for the whole WebUI process (every other extension included). The session is
# created on demand when the user enables "Use cache" and torn down when they
# disable it; while disabled, _booru_requester() returns the plain `requests`
# module so fetches are uncached but still go out normally.
_BOORU_SESSION = None


def enable_booru_cache(enabled):
    """Create or tear down the booru-only cached session."""
    global _BOORU_SESSION
    if enabled:
        if _BOORU_SESSION is None:
            _BOORU_SESSION = requests_cache.CachedSession(
                'ranbooru_cache',
                backend='sqlite',
                expire_after=3600,
                ignored_parameters=['api_key', 'user_id'],
            )
    else:
        if _BOORU_SESSION is not None:
            try:
                _BOORU_SESSION.close()
            except Exception:
                pass
            _BOORU_SESSION = None


def _booru_requester():
    """Return the cached session if caching is on, else the plain requests module.
    Both expose a compatible .get(), so call sites don't change shape."""
    return _BOORU_SESSION if _BOORU_SESSION is not None else requests
RATING_TYPES = {
    "none": {
        "All": "All"
    },
    "full": {
        "All": "All",
        "Safe": "safe",
        "Questionable": "questionable",
        "Explicit": "explicit"
    },
    "single": {
        "All": "All",
        "Safe": "g",
        "Sensitive": "s",
        "Questionable": "q",
        "Explicit": "e"
    }
}
RATINGS = {
    "e621": RATING_TYPES['full'],
    "danbooru": RATING_TYPES['single'],
    "aibooru": RATING_TYPES['full'],
    "yande.re": RATING_TYPES['full'],
    "konachan": RATING_TYPES['full'],
    "safebooru": RATING_TYPES['none'],
    "rule34": RATING_TYPES['full'],
    "xbooru": RATING_TYPES['full'],
    "gelbooru": RATING_TYPES['single']
}


def get_available_ratings(booru):
    # gr.update (not gr.Radio.update) - the class-method form was removed in
    # Gradio 4.x, which Forge Neo uses. list() the keys so choices is concrete.
    return gr.update(choices=list(RATINGS[booru].keys()), value="All")


def show_fringe_benefits(booru):
    return gr.update(visible=(booru == 'gelbooru'))


def check_exception(booru, parameters):
    post_id = parameters.get('post_id')
    tags = parameters.get('tags')
    if booru == 'konachan' and post_id:
        raise Exception("Konachan does not support post IDs")
    if booru == 'yande.re' and post_id:
        raise Exception("Yande.re does not support post IDs")
    if booru == 'e621' and post_id:
        raise Exception("e621 does not support post IDs")
    if booru == 'danbooru' and len(tags.split(',')) > 1:
        raise Exception("Danbooru does not support multiple tags. You can have only one tag.")


class Booru():

    def __init__(self, booru, booru_url):
        self.booru = booru
        # Pristine base URL. Per-request URLs are built from this and NEVER
        # written back, so calling get_data twice on one instance (as the
        # mix-from-presets path does) can't accumulate stale query params.
        self._base_url = booru_url
        self.booru_url = booru_url  # last-built URL, for logging only
        self.headers = {'user-agent': 'my-app/0.0.1'}

    def _request_url(self, extra=''):
        """Build a request URL from the pristine base without mutating state."""
        url = f"{self._base_url}{extra}"
        self.booru_url = url  # record for the (sanitised) debug print only
        return url

    def _get(self, url):
        """Single choke point for GETs: always sets headers and a timeout, and
        routes through the booru cache session when caching is enabled."""
        return _booru_requester().get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)

    def get_data(self, add_tags, max_pages=10, id=''):
        pass

    def get_post(self, add_tags, max_pages=10, id=''):
        pass


class Gelbooru(Booru):

    def __init__(self, fringe_benefits, api_key=None, user_id=None):
        super().__init__('gelbooru', f'https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit={POST_AMOUNT}')
        self.fringeBenefits = fringe_benefits
        self.api_key = api_key
        self.user_id = user_id

    def _credentials(self):
        """Credentials as a params dict, so they are never baked into the
        logged URL string (self.booru_url) or the requests_cache key text."""
        if self.api_key and self.user_id:
            return {'api_key': self.api_key, 'user_id': self.user_id}
        return {}

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        if id:
            add_tags = ''

        base_url = self._base_url
        cookies = {'fringeBenefits': 'yup'} if self.fringeBenefits else None
        creds = self._credentials()

        def fetch(pid):
            # Build the URL fresh each attempt - never mutate the base. The
            # api_key/user_id go through params=, so they do NOT appear in the
            # URL we store for logging.
            url = f"{base_url}&pid={pid}{id}{add_tags}"
            self.booru_url = url  # recorded for the (credential-free) debug print
            res = _booru_requester().get(url, params=creds, cookies=cookies,
                                         headers=self.headers, timeout=REQUEST_TIMEOUT)
            try:
                return res.json()
            except ValueError:
                print(f"Ranbooru: Gelbooru returned a non-JSON response (status {res.status_code}).")
                return {}

        # randint is randrange(a, b+1), so pass max_pages-1
        pid = random.randint(0, max(0, max_pages - 1))
        data = fetch(pid)
        COUNT = data.get('@attributes', {}).get('count', 0) if isinstance(data, dict) else 0

        # If fewer results exist than the requested page range, re-roll within the actual range
        if COUNT <= max_pages * POST_AMOUNT:
            actual_pages = max(1, COUNT // POST_AMOUNT + 1)
            print(f" Processing {COUNT} results.")
            if actual_pages != max_pages:
                pid = random.randint(0, actual_pages - 1)
                data = fetch(pid)
        else:
            print(f" Processing {max_pages * POST_AMOUNT} out of {COUNT} results.")

        # If the chosen page came back empty, re-roll within the known-good
        # range first, then fall back to a geometric walk-down. A linear
        # decrement from a high pid (now that Max Pages can be 1000) could
        # exhaust its retries far from any real results; re-rolling inside
        # actual_pages lands in the populated range immediately.
        if not (isinstance(data, dict) and data.get('post')) and COUNT > 0:
            good_pages = max(1, COUNT // POST_AMOUNT + 1)
            retries = 0
            while not (isinstance(data, dict) and data.get('post')) and retries < 8:
                retries += 1
                pid = random.randint(0, good_pages - 1)
                print(f"Ranbooru: empty page, re-rolling within {good_pages} known page(s) (try {retries})")
                data = fetch(pid)
        return data if isinstance(data, dict) else {}

    def get_post(self, add_tags, max_pages=10, id=''):
        return self.get_data(add_tags, max_pages, "&id=" + id)

    def filter_character_tags(self, tag_string):
        """ Given a space-separated tag string from a Gelbooru post, query the
        Tag List API in a single batched request and strip out any tags whose
        type is 4 (character). Returns the filtered tag string.
        Falls back to the original string on any error.
        """
        if not tag_string or not tag_string.strip():
            return tag_string
        try:
            tags = [t for t in tag_string.split(' ') if t]
            base = 'https://gelbooru.com/index.php'
            cookies = {'fringeBenefits': 'yup'} if self.fringeBenefits else None
            creds = self._credentials()
            character_names = set()
            # The tag API caps at 100 names per request, so page through the
            # post's tags in chunks of 100 rather than silently dropping the
            # rest on heavily tagged posts. requests builds the query string,
            # so tags containing &, +, # or % are encoded correctly.
            for start in range(0, len(tags), 100):
                chunk = tags[start:start + 100]
                params = {
                    'page': 'dapi', 's': 'tag', 'q': 'index', 'json': '1',
                    'names': ' '.join(chunk), 'limit': 100, **creds,
                }
                res = _booru_requester().get(base, params=params, cookies=cookies,
                                             headers=self.headers, timeout=REQUEST_TIMEOUT)
                if res.status_code != 200 or not res.text.strip():
                    print(f"Ranbooru: character tag filter request failed (status {res.status_code}), skipping filter")
                    return tag_string
                entries = res.json().get('tag', [])
                character_names.update(e['name'] for e in entries if e.get('type') == 4)
            if not character_names:
                return tag_string
            filtered = [t for t in tags if t not in character_names]
            removed = len(tags) - len(filtered)
            if removed > 0:
                print(f"Ranbooru: filtered {removed} character tag(s)")
            return ' '.join(filtered)
        except Exception:
            print("Ranbooru: error while filtering character tags, using unfiltered tags")
            return tag_string


class XBooru(Booru):

    def __init__(self):
        super().__init__('xbooru', f'https://xbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&pid={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            COUNT = 0
            for post in data:
                post['file_url'] = f"https://xbooru.com/images/{post['directory']}/{post['image']}"
                COUNT += 1
            if COUNT <= max_pages*POST_AMOUNT:
                max_pages = COUNT // POST_AMOUNT+1
                # If max_pages is bigger than available pages, loop the function with updated max_pages based on the value of COUNT
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f" Processing {max_pages*POST_AMOUNT} out of {COUNT} results.")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        return self.get_data(add_tags, max_pages, "&id=" + id)


class Rule34(Booru):

    def __init__(self):
        super().__init__('rule34', f'https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&pid={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            COUNT = len(data)
            if COUNT == 0:
                max_pages = 2
                # Rule34 does not have a way to know the amount of results available in the search, so we need to run the function again with a fixed amount of pages
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f"Found enough results")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        return self.get_data(add_tags, max_pages, "&id=" + id)


class Safebooru(Booru):

    def __init__(self):
        super().__init__('safebooru', f'https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&pid={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            COUNT = 0
            for post in data:
                post['file_url'] = f"https://safebooru.org/images/{post['directory']}/{post['image']}"
                COUNT += 1
            if COUNT <= max_pages*POST_AMOUNT:
                max_pages = COUNT // POST_AMOUNT+1
                # If max_pages is bigger than available pages, loop the function with updated max_pages based on the value of COUNT
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f" Processing {max_pages*POST_AMOUNT} out of {COUNT} results.")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        return self.get_data(add_tags, max_pages, "&id=" + id)


class Konachan(Booru):

    def __init__(self):
        super().__init__('konachan', f'https://konachan.com/post.json?limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&page={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            COUNT = len(data)
            if COUNT == 0:
                max_pages = 2
                # Konachan does not have a way to know the amount of results available in the search, so we need to run the function again with a fixed amount of pages
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f"Found enough results")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        raise Exception("Konachan does not support post IDs")


class Yandere(Booru):

    def __init__(self):
        super().__init__('yande.re', f'https://yande.re/post.json?limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&page={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            COUNT = len(data)
            if COUNT == 0:
                max_pages = 2
                # Yandere does not have a way to know the amount of results available in the search, so we need to run the function again with a fixed amount of pages
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f"Found enough results")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        raise Exception("Yande.re does not support post IDs")


class AIBooru(Booru):

    def __init__(self):
        super().__init__('AIBooru', f'https://aibooru.online/posts.json?limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&page={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            for post in data:
                post['tags'] = post['tag_string']
            COUNT = len(data)
            if COUNT == 0:
                max_pages = 2
                # AIBooru does not have a way to know the amount of results available in the search, so we need to run the function again with a fixed amount of pages
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f"Found enough results")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        raise Exception("AIBooru does not support post IDs")


class Danbooru(Booru):

    def __init__(self):
        super().__init__('danbooru', f'https://danbooru.donmai.us/posts.json?limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&page={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()
            for post in data:
                post['tags'] = post['tag_string']
            COUNT = len(data)
            if COUNT == 0:
                max_pages = 2
                # Danbooru does not have a way to know the amount of results available in the search, so we need to run the function again with a fixed amount of pages
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f"Found enough results")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        url = f"https://danbooru.donmai.us/posts/{id}.json"
        self.booru_url = url
        res = self._get(url)
        data = res.json()
        data['tags'] = data['tag_string']
        data = {'post': [data]}
        return data


class e621(Booru):

    def __init__(self):
        super().__init__('e621', f'https://e621.net/posts.json?limit={POST_AMOUNT}')

    def get_data(self, add_tags, max_pages=10, id=''):
        global COUNT
        loop_msg = True # avoid showing same msg twice
        for loop in range(2): # run loop at most twice
            if id:
                add_tags = ''
            url = self._request_url(f"&page={random.randint(0, max_pages-1)}{id}{add_tags}")
            res = self._get(url)
            data = res.json()['posts']
            COUNT = len(data)
            for post in data:
                temp_tags = []
                sublevels = ['general', 'artist', 'copyright', 'character', 'species']
                for sublevel in sublevels:
                    temp_tags.extend(post['tags'][sublevel])
                post['tags'] = ' '.join(temp_tags)
                post['score'] = post['score']['total']
            if COUNT <= max_pages*POST_AMOUNT:
                max_pages = COUNT // POST_AMOUNT+1
                # If max_pages is bigger than available pages, loop the function with updated max_pages based on the value of COUNT
                while loop_msg:
                    print(f" Processing {COUNT} results.")
                    loop_msg = False
                    # avoid showing same msg twice
                continue
            else:
                print(f" Processing {max_pages*POST_AMOUNT} out of {COUNT} results.")
            break
        return {'post': data}

    def get_post(self, add_tags, max_pages=10, id=''):
        return self.get_data(add_tags, max_pages, "&id=" + id)


def generate_chaos(pos_tags, neg_tags, chaos_amount):
    """Generates chaos in the prompt by adding random tags from the prompt to the positive and negative prompts

    Args:
        pos_tags (str): the positive prompt
        neg_tags (str): the negative prompt
        chaos_amount (float): the percentage of tags to put in the positive prompt

    Returns:
        str: the positive prompt
        str: the negative prompt
    """
    # create a list with the tags in the prompt and in the negative prompt
    chaos_list = [tag for tag in pos_tags.split(',') + neg_tags.split(',') if tag.strip() != '']
    # distinct the list
    chaos_list = list(set(chaos_list))
    random.shuffle(chaos_list)
    # put 50% of the tags in the prompt and the remaining 50% in the negative prompt
    len_list = round(len(chaos_list) * chaos_amount)
    pos_list = chaos_list[len_list:]
    pos_prompt = ','.join(pos_list)
    neg_list = chaos_list[:len_list]
    random.shuffle(neg_list)
    neg_prompt = ','.join(neg_list)
    return pos_prompt, neg_prompt


def resize_image(img, width, height, cropping=True):
    """Resize image to specified width and height

    Args:
        img (PIL.Image): the image
        width (int): the width in pixels
        height (int): the height in pixels
        cropping (bool): whether to crop the image or not

    Returns:
        PIL.Image: the resized image
    """
    if cropping:
        # resize the picture and center crop it
        # example: you have a 100x200 picture and width=300 and height=300
        # resize to 300x600 and crop to 300x300 from the center
        x, y = img.size
        if x < y:
            # scale to width keeping aspect ratio
            wpercent = (width / float(img.size[0]))
            hsize = int((float(img.size[1]) * float(wpercent)))
            img_new = img.resize((width, hsize))
            if img_new.size[1] < height:
                # scale to height keeping aspect ratio
                hpercent = (height / float(img.size[1]))
                wsize = int((float(img.size[0]) * float(hpercent)))
                img_new = img.resize((wsize, height))
        else:
            ypercent = (height / float(img.size[1]))
            wsize = int((float(img.size[0]) * float(ypercent)))
            img_new = img.resize((wsize, height))
            if img_new.size[0] < width:
                xpercent = (width / float(img.size[0]))
                hsize = int((float(img.size[1]) * float(xpercent)))
                img_new = img.resize((width, hsize))

        # crop center
        x, y = img_new.size
        left = (x - width) / 2
        top = (y - height) / 2
        right = (x + width) / 2
        bottom = (y + height) / 2
        img = img_new.crop((left, top, right, bottom))
    else:
        img = img.resize((width, height))
    return img

def modify_prompt(prompt, tagged_prompt, type_deepbooru):
    """Modifies the prompt based on the type_deepbooru selected

    Args:
        prompt (str): the prompt
        tagged_prompt (str): the prompt tagged by deepbooru
        type_deepbooru (str): the type of modification

    Returns:
        str: the modified prompt
    """
    if type_deepbooru == 'Add Before':
        return tagged_prompt + ',' + prompt
    elif type_deepbooru == 'Add After':
        return prompt + ',' + tagged_prompt
    elif type_deepbooru == 'Replace':
        return tagged_prompt
    return prompt

def remove_repeated_tags(prompt):
    """Removes the repeated tags keeping the same order

    Args:
        prompt (str): the prompt

    Returns:
        str: the prompt without repeated tags
    """
    prompt = prompt.split(',')
    new_prompt = []
    for tag in prompt:
        if tag not in new_prompt:
            new_prompt.append(tag)
    return ','.join(new_prompt)

def limit_prompt_tags(prompt, limit_tags, mode):
    """Limits the amount of tags in the prompt. It can be done by percentage or by a fixed amount.

    Args:
        prompt (str): the prompt
        limit_tags (float): the percentage of tags to keep
        mode (str): 'Limit' or 'Max'

    Returns:
        str: the prompt with the limited amount of tags
    """
    clean_prompt = prompt.split(',')
    if mode == 'Limit':
        clean_prompt = clean_prompt[:int(len(clean_prompt) * limit_tags)]
    elif mode == 'Max':
        clean_prompt = clean_prompt[:limit_tags]
    return ','.join(clean_prompt)

class Script(scripts.Script):
    previous_loras = ''
    last_img = []
    real_steps = 0
    version = "1.2"
    original_prompt = ''

    def get_files(self, path):
        files = []
        for file in os.listdir(path):
            if file.endswith('.txt'):
                files.append(file)
        return files

    def hide_object(self, obj, booru):
        print(f'hide_object: {obj}, {booru.value}')
        if booru.value == 'konachan' or booru.value == 'yande.re':
            obj.interactive = False
        else:
            obj.interactive = True

    def title(self):
        return "Ranbooru"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def show_gelbooru_api_fields(self, booru):
        """Show/hide API key fields based on selected booru"""
        if booru == 'gelbooru':
            return gr.update(visible=True)
        else:
            return gr.update(visible=False)
    
    def load_gelbooru_credentials(self, booru):
        """Load saved credentials for Gelbooru"""
        if booru == 'gelbooru':
            credentials = credentials_manager.get_booru_credentials('gelbooru')
            api_key = credentials.get('api_key', '')
            user_id = credentials.get('user_id', '')
            has_saved_creds = credentials_manager.has_credentials('gelbooru')
            
            if has_saved_creds:
                # Hide input fields and show file path
                credentials_file_path = credentials_manager.credentials_file
                status_text = f"✓ Credentials loaded from: {credentials_file_path}"
                return (
                    gr.update(visible=False),  # api_key field
                    gr.update(visible=False),  # user_id field  
                    gr.update(visible=True, value=status_text),  # credentials_status
                    gr.update(visible=True, value="Clear saved credentials")  # clear_credentials_btn
                )
            else:
                # Show input fields
                return (
                    gr.update(visible=True, value=api_key),  # api_key field
                    gr.update(visible=True, value=user_id),  # user_id field
                    gr.update(visible=False, value=""),  # credentials_status
                    gr.update(visible=False)  # clear_credentials_btn
                )
        else:
            return (
                gr.update(visible=False, value=""),  # api_key field
                gr.update(visible=False, value=""),  # user_id field
                gr.update(visible=False, value=""),  # credentials_status
                gr.update(visible=False)  # clear_credentials_btn
            )
    
    def clear_gelbooru_credentials(self):
        """Clear saved Gelbooru credentials"""
        credentials_manager.clear_booru_credentials('gelbooru')
        return (
            gr.update(visible=True, value=""),  # api_key field
            gr.update(visible=True, value=""),  # user_id field  
            gr.update(visible=False, value=""),  # credentials_status
            gr.update(visible=False)  # clear_credentials_btn
        )

    def save_gelbooru_credentials(self, booru, api_key, user_id, save_credentials):
        """Save Gelbooru credentials if checkbox is checked"""
        if booru == 'gelbooru' and save_credentials and api_key.strip() and user_id.strip():
            credentials_manager.save_booru_credentials('gelbooru', api_key.strip(), user_id.strip())
            return "✓ Credentials saved"
        elif booru == 'gelbooru' and not save_credentials:
            # Optionally clear credentials if save is unchecked
            return "Credentials will not be saved"
        return ""

    def refresh_ser(self):
        return gr.update(choices=self.get_files(user_search_dir))
    def refresh_rem(self):
        return gr.update(choices=self.get_files(user_remove_dir))

    def ui(self, is_img2img):
        # Determine initial Gelbooru credential visibility based on saved credentials
        has_saved = credentials_manager.has_credentials('gelbooru')
        saved_creds = credentials_manager.get_booru_credentials('gelbooru') if has_saved else {}
        initial_api_key_visible = not has_saved
        initial_user_id_visible = not has_saved
        initial_status_visible = has_saved
        initial_status_value = f"✓ Credentials loaded from: {credentials_manager.credentials_file}" if has_saved else ""
        initial_clear_visible = has_saved
        
        with InputAccordion(False, label="Ranbooru", elem_id=self.elem_id("ra_enable")) as enabled:
            booru = gr.Dropdown(
                ["safebooru", "rule34", "danbooru", "gelbooru", "konachan", 'yande.re', 'aibooru', 'xbooru', 'e621'], label="Booru", value=DEFAULT_BOORU)
            max_pages = gr.Slider(label="Max Pages", minimum=1, maximum=1000, value=100, step=1)
            gr.Markdown("""## Post""")
            post_id = gr.Textbox(lines=1, label="Post ID")
            gr.Markdown("""## Tags""")
            tags = gr.Textbox(lines=1, label="Tags to Search (Pre)")
            remove_tags = gr.Textbox(lines=1, label="Tags to Remove (Post)")
            filter_character_tags = gr.Checkbox(label="Filter Character Tags (Gelbooru only)", value=False, info="Removes character name tags via an extra API lookup. Adds one request per generation.")
            skip_existing_tags = gr.Checkbox(label="Skip tags already in prompt", value=False, info="Don't add fetched tags that already appear in your prompt")
            with gr.Accordion("Tag Presets", open=False):
                use_random_preset = gr.Checkbox(label="Pick a random preset per generation (overrides Tags field above)", value=False)
                with gr.Row():
                    tag_preset_1 = gr.Textbox(lines=1, label="Preset 1")
                    preset_max_pages_1 = gr.Number(value=0, label="Max Pages", precision=0, minimum=0)
                with gr.Row():
                    tag_preset_2 = gr.Textbox(lines=1, label="Preset 2")
                    preset_max_pages_2 = gr.Number(value=0, label="Max Pages", precision=0, minimum=0)
                with gr.Row():
                    tag_preset_3 = gr.Textbox(lines=1, label="Preset 3")
                    preset_max_pages_3 = gr.Number(value=0, label="Max Pages", precision=0, minimum=0)
                with gr.Row():
                    tag_preset_4 = gr.Textbox(lines=1, label="Preset 4")
                    preset_max_pages_4 = gr.Number(value=0, label="Max Pages", precision=0, minimum=0)
                with gr.Row():
                    tag_preset_5 = gr.Textbox(lines=1, label="Preset 5")
                    preset_max_pages_5 = gr.Number(value=0, label="Max Pages", precision=0, minimum=0)
                gr.Markdown("_Max Pages 0 = use global Max Pages setting_")
            mature_rating = gr.Radio(list(RATINGS['safebooru']), label="Mature Rating", value="All")
            remove_bad_tags = gr.Checkbox(label="Remove bad tags", value=True)
            shuffle_tags = gr.Checkbox(label="Shuffle tags", value=True)
            change_dash = gr.Checkbox(label='Convert "_" to spaces', value=False)
            same_prompt = gr.Checkbox(label="Use same prompt for all images", value=False)
            # Fringe Benefits and the credentials group are Gelbooru-only. Their
            # initial visibility matches the default booru (safebooru) so they
            # start hidden; the booru.change handlers below reveal them when
            # Gelbooru is selected. (Previously both rendered visible at load
            # regardless of the default, since .change only fires on change.)
            _default_is_gelbooru = (DEFAULT_BOORU == 'gelbooru')
            fringe_benefits = gr.Checkbox(label="Fringe Benefits", value=True, visible=_default_is_gelbooru)
            # API credentials for Gelbooru
            with gr.Group(visible=_default_is_gelbooru) as gelbooru_credentials_group:
                gr.Markdown("### Gelbooru API Credentials")
                api_key = gr.Textbox(
                    lines=1, label="API Key", placeholder="Enter your Gelbooru API key",
                    type="password", visible=initial_api_key_visible, value=saved_creds.get('api_key', '')
                )
                user_id = gr.Textbox(
                    lines=1, label="User ID", placeholder="Enter your Gelbooru user ID",
                    visible=initial_user_id_visible, value=saved_creds.get('user_id', '')
                )
                save_credentials = gr.Checkbox(label="Save credentials", value=False, visible=True)
                credentials_status = gr.Textbox(
                    label="Status", interactive=False,
                    visible=True, value=initial_status_value
                )
                clear_credentials_btn = gr.Button(
                    "Clear saved credentials", visible=initial_clear_visible
                )
            
            limit_tags = gr.Slider(value=1.0, label="Limit tags", minimum=0.05, maximum=1.0, step=0.05)
            max_tags = gr.Slider(value=100, label="Max tags", minimum=1, maximum=100, step=1)
            change_background = gr.Radio(["Don't Change", "Add Background", "Remove Background", "Remove All"], label="Change Background", value="Don't Change")
            change_color = gr.Radio(["Don't Change", "Colored", "Limited Palette", "Monochrome"], label="Change Color", value="Don't Change")
            sorting_order = gr.Radio(["Random", "High Score", "Low Score"], label="Sorting Order", value="Random")            
            booru.change(get_available_ratings, booru, mature_rating)  # update available ratings
            booru.change(show_fringe_benefits, booru, fringe_benefits)  # display fringe benefits checkbox if gelbooru is selected
            booru.change(self.show_gelbooru_api_fields, booru, gelbooru_credentials_group)  # show/hide gelbooru credentials group

            gr.Markdown("""\n---\n""")
            with gr.Group():
                with gr.Accordion("Img2Img", open=False):
                    use_img2img = gr.Checkbox(label="Use img2img", value=False)
                    use_ip = gr.Checkbox(label="Send to Controlnet", value=False)
                    denoising = gr.Slider(value=0.75, label="Denoising", minimum=0.05, maximum=1.0, step=0.05)
                    use_last_img = gr.Checkbox(label="Use last image as img2img", value=False)
                    crop_center = gr.Checkbox(label="Crop Center", value=False)
                    use_deepbooru = gr.Checkbox(label="Use Deepbooru", value=False)
                    type_deepbooru = gr.Radio(["Add Before", "Add After", "Replace"], label="Deepbooru Tags Position", value="Add Before")
            with gr.Group():
                with gr.Accordion("File", open=False):
                    use_search_txt = gr.Checkbox(label="Use tags_search.txt", value=False)
                    choose_search_txt = gr.Dropdown(self.get_files(user_search_dir), label="Choose tags_search.txt", value="")
                    search_refresh_btn = gr.Button("Refresh")
                    use_remove_txt = gr.Checkbox(label="Use tags_remove.txt", value=False)
                    choose_remove_txt = gr.Dropdown(self.get_files(user_remove_dir), label="Choose tags_remove.txt", value="")
                    remove_refresh_btn = gr.Button("Refresh")
            with gr.Group():
                with gr.Accordion("Extra", open=False):
                    with gr.Box():
                        mix_prompt = gr.Checkbox(label="Mix prompts", value=False)
                        mix_amount = gr.Slider(value=2, label="Mix amount", minimum=2, maximum=10, step=1)
                        mix_ratio = gr.Slider(value=0.5, label="Mix ratio", minimum=0.0, maximum=1.0, step=0.05, info="0 = all tags from image 1, 1 = all tags from image 2, 0.5 = even split")
                        mix_from_presets = gr.Checkbox(label="Mix from presets", value=False, interactive=False, info="Image 2 is drawn from a random Tag Preset's search. Requires random presets enabled.")
                        use_random_preset.change(fn=lambda x: gr.update(interactive=x, value=False) if not x else gr.update(interactive=True), inputs=[use_random_preset], outputs=[mix_from_presets])
                    with gr.Box():
                        chaos_mode = gr.Radio(["None", "Chaos", "Less Chaos"], label="Chaos Mode", value="None")
                        chaos_amount = gr.Slider(value=0.5, label="Chaos Amount %", minimum=0.1, maximum=1, step=0.05)
                    with gr.Box():
                        negative_mode = gr.Radio(["None", "Negative"], label="Negative Mode", value="None")
                        use_same_seed = gr.Checkbox(label="Use same seed for all pictures", value=False)
                    with gr.Box():
                        use_cache = gr.Checkbox(label="Use cache", value=True)
        with InputAccordion(False, label="LoRAnado", elem_id=self.elem_id("lo_enable")) as lora_enabled:
            with gr.Box():
                lora_lock_prev = gr.Checkbox(label="Lock previous LoRAs", value=False)
                lora_folder = gr.Textbox(lines=1, label="LoRAs Subfolder")
                lora_amount = gr.Slider(value=1, label="LoRAs Amount", minimum=1, maximum=10, step=1)
            with gr.Box():
                lora_min = gr.Slider(value=-1.0, label="Min LoRAs Weight", minimum=-2.0, maximum=2.0, step=0.1)
                lora_max = gr.Slider(value=1.0, label="Max LoRAs Weight", minimum=-2.0, maximum=2.0, step=0.1)
                lora_custom_weights = gr.Textbox(lines=1, label="LoRAs Custom Weights")
            lora_folder_2_enabled = gr.Checkbox(label="Enable second LoRA folder", value=False)
            with gr.Box(visible=False) as lora_folder_2_group:
                lora_folder_2 = gr.Textbox(lines=1, label="LoRAs Subfolder 2")
                lora_amount_2 = gr.Slider(value=1, label="LoRAs Amount 2", minimum=1, maximum=10, step=1)
                lora_min_2 = gr.Slider(value=-1.0, label="Min LoRAs Weight 2", minimum=-2.0, maximum=2.0, step=0.1)
                lora_max_2 = gr.Slider(value=1.0, label="Max LoRAs Weight 2", minimum=-2.0, maximum=2.0, step=0.1)
                lora_custom_weights_2 = gr.Textbox(lines=1, label="LoRAs Custom Weights 2")
            lora_folder_2_enabled.change(
                fn=lambda x: gr.update(visible=x),
                inputs=[lora_folder_2_enabled],
                outputs=[lora_folder_2_group]
            )

        search_refresh_btn.click(
            fn=self.refresh_ser,
            inputs=[],
            outputs=[choose_search_txt]
        )

        remove_refresh_btn.click(
            fn=self.refresh_rem,
            inputs=[],
            outputs=[choose_remove_txt]
        )

        # Event handler for loading credentials when booru changes
        booru.change(
            fn=self.load_gelbooru_credentials,
            inputs=[booru],
            outputs=[api_key, user_id, credentials_status, clear_credentials_btn]
        )

        # Event handler for clearing credentials
        clear_credentials_btn.click(
            fn=self.clear_gelbooru_credentials,
            inputs=[],
            outputs=[api_key, user_id, credentials_status, clear_credentials_btn]
        )

        return [enabled, tags, booru, remove_bad_tags, max_pages, change_dash, same_prompt, fringe_benefits, remove_tags, filter_character_tags, skip_existing_tags, use_img2img, denoising, use_last_img, change_background, change_color, shuffle_tags, post_id, mix_prompt, mix_amount, mix_ratio, mix_from_presets, chaos_mode, negative_mode, chaos_amount, limit_tags, max_tags, sorting_order, mature_rating, lora_folder, lora_amount, lora_min, lora_max, lora_enabled, lora_custom_weights, lora_lock_prev, lora_folder_2_enabled, lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2, use_ip, use_search_txt, use_remove_txt, choose_search_txt, choose_remove_txt, search_refresh_btn, remove_refresh_btn, crop_center, use_deepbooru, type_deepbooru, use_same_seed, use_cache, api_key, user_id, save_credentials, credentials_status, clear_credentials_btn, use_random_preset, tag_preset_1, tag_preset_2, tag_preset_3, tag_preset_4, tag_preset_5, preset_max_pages_1, preset_max_pages_2, preset_max_pages_3, preset_max_pages_4, preset_max_pages_5]

    def check_orientation(self, img):
        """Check if image is portrait, landscape or square"""
        x, y = img.size
        if x / y > 1.2:
            return [768, 512]
        elif y / x > 1.2:
            return [512, 768]
        else:
            return [768, 768]

    def _build_lora_segment(self, lora_folder, lora_amount, lora_min, lora_max, lora_custom_weights):
        """ Build a lora prompt segment for a single folder config.
        Returns '' if the folder is empty/invalid so it contributes nothing. """
        segment = ''
        if not lora_folder or not lora_folder.strip():
            return segment
        try:
            loras = os.listdir(f'{lora_folder}')
        except Exception:
            print(f'Ranbooru: LoRA folder "{lora_folder}" could not be read, skipping')
            return segment
        loras = [lora.replace('.safetensors', '') for lora in loras if lora.endswith('.safetensors')]
        if not loras:
            print(f'Ranbooru: no .safetensors found in "{lora_folder}", skipping')
            return segment
        custom = [w.strip() for w in lora_custom_weights.split(',')] if lora_custom_weights.strip() else []
        for l in range(0, lora_amount):
            lora_weight = 0.0
            if l < len(custom) and custom[l] != '':
                try:
                    lora_weight = float(custom[l])
                except ValueError:
                    print(f'Ranbooru: invalid custom LoRA weight "{custom[l]}", using a random weight')
                    lora_weight = 0.0
            if lora_weight == 0:
                # Draw a non-zero random weight, but don't spin forever when the
                # range is degenerate (min == max == 0, or a zero-width range).
                for _ in range(20):
                    lora_weight = round(random.uniform(lora_min, lora_max), 1)
                    if lora_weight != 0:
                        break
                else:
                    # Range can't produce a non-zero value; fall back so the LoRA
                    # still applies at a sane weight instead of hanging.
                    lora_weight = lora_max if lora_max != 0 else (lora_min if lora_min != 0 else 1.0)
            segment += f'<lora:{random.choice(loras)}:{lora_weight}>'
        return segment

    def loranado(self, lora_enabled, lora_folder, lora_amount, lora_min, lora_max, lora_custom_weights, p, lora_lock_prev,
                 lora_folder_2_enabled=False, lora_folder_2='', lora_amount_2=1, lora_min_2=-1.0, lora_max_2=1.0, lora_custom_weights_2=''):
        lora_prompt = ''
        if lora_enabled:
            if lora_lock_prev:
                lora_prompt = self.previous_loras
            else:
                lora_prompt += self._build_lora_segment(lora_folder, lora_amount, lora_min, lora_max, lora_custom_weights)
                if lora_folder_2_enabled:
                    lora_prompt += self._build_lora_segment(lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2)
                self.previous_loras = lora_prompt
        if lora_prompt:
            if isinstance(p.prompt, list):
                for num, pr in enumerate(p.prompt):
                    p.prompt[num] = f'{lora_prompt} {pr}'
            else:
                p.prompt = f'{lora_prompt} {p.prompt}'
        return p

    def before_process(self, p, enabled, tags, booru, remove_bad_tags, max_pages, change_dash, same_prompt, fringe_benefits, remove_tags, filter_character_tags, skip_existing_tags, use_img2img, denoising, use_last_img, change_background, change_color, shuffle_tags, post_id, mix_prompt, mix_amount, mix_ratio, mix_from_presets, chaos_mode, negative_mode, chaos_amount, limit_tags, max_tags, sorting_order, mature_rating, lora_folder, lora_amount, lora_min, lora_max, lora_enabled, lora_custom_weights, lora_lock_prev, lora_folder_2_enabled, lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2, use_ip, use_search_txt, use_remove_txt, choose_search_txt, choose_remove_txt, search_refresh_btn, remove_refresh_btn, crop_center, use_deepbooru, type_deepbooru, use_same_seed, use_cache, api_key, user_id, save_credentials, credentials_status, clear_credentials_btn, use_random_preset=False, tag_preset_1='', tag_preset_2='', tag_preset_3='', tag_preset_4='', tag_preset_5='', preset_max_pages_1=0, preset_max_pages_2=0, preset_max_pages_3=0, preset_max_pages_4=0, preset_max_pages_5=0):        # Manage Cache
        # Toggle the booru-only cache session. Unlike install_cache(), this does
        # not patch requests globally, so other extensions' HTTP is untouched.
        enable_booru_cache(use_cache)

        if enabled:
            # Tag Preset: pick a random non-empty preset to override the tags field
            if use_random_preset:
                all_presets = [tag_preset_1, tag_preset_2, tag_preset_3, tag_preset_4, tag_preset_5]
                all_preset_pages = [preset_max_pages_1, preset_max_pages_2, preset_max_pages_3, preset_max_pages_4, preset_max_pages_5]
                # Carry the index with the text so duplicate preset strings don't
                # collapse to the first match's Max Pages override.
                candidates = [(i, pr) for i, pr in enumerate(all_presets) if pr.strip()]
                if candidates:
                    chosen_index, tags = random.choice(candidates)
                    preset_pages = all_preset_pages[chosen_index]
                    if preset_pages and int(preset_pages) > 0:
                        max_pages = int(preset_pages)
                        print(f'Ranbooru: using random tag preset: "{tags}" with max_pages={max_pages}')
                    else:
                        print(f'Ranbooru: using random tag preset: "{tags}" (global max_pages={max_pages})')
                else:
                    print('Ranbooru: random preset enabled but no presets defined, falling back to Tags field')

            # Handle Gelbooru credentials
            gelbooru_api_key = None
            gelbooru_user_id = None
            
            if booru == 'gelbooru':
                # Use provided credentials or load from saved credentials
                if api_key.strip() and user_id.strip():
                    gelbooru_api_key = api_key.strip()
                    gelbooru_user_id = user_id.strip()
                    
                    # Save credentials if checkbox is checked
                    if save_credentials:
                        credentials_manager.save_booru_credentials('gelbooru', gelbooru_api_key, gelbooru_user_id)
                else:
                    # Try to load saved credentials
                    saved_credentials = credentials_manager.get_booru_credentials('gelbooru')
                    gelbooru_api_key = saved_credentials.get('api_key', '')
                    gelbooru_user_id = saved_credentials.get('user_id', '')
            
            # Initialize APIs
            booru_apis = {
                'gelbooru': Gelbooru(fringe_benefits, gelbooru_api_key, gelbooru_user_id),
                'rule34': Rule34(),
                'safebooru': Safebooru(),
                'danbooru': Danbooru(),
                'konachan': Konachan(),
                'yande.re': Yandere(),
                'aibooru': AIBooru(),
                'xbooru': XBooru(),
                'e621': e621(),
            }
            self.original_prompt = p.prompt
            # Check if compatible
            check_exception(booru, {'tags': tags, 'post_id': post_id})

            # Manage Bad Tags
            bad_tags = []
            if remove_bad_tags:
                bad_tags = ['mixed-language_text', 'watermark', 'text', 'english_text', 'speech_bubble', 'signature', 'artist_name', 'censored', 'bar_censor', 'translation', 'twitter_username', "twitter_logo", 'patreon_username', 'commentary_request', 'tagme', 'commentary', 'character_name', 'mosaic_censoring', 'instagram_username', 'text_focus', 'english_commentary', 'comic', 'translation_request', 'fake_text', 'translated', 'paid_reward_available', 'thought_bubble', 'multiple_views', 'silent_comic', 'out-of-frame_censoring', 'symbol-only_commentary', '3koma', '2koma', 'character_watermark', 'spoken_question_mark', 'japanese_text', 'spanish_text', 'language_text', 'fanbox_username', 'commission', 'original', 'ai_generated', 'stable_diffusion', 'tagme_(artist)', 'text_bubble', 'qr_code', 'chinese_commentary', 'korean_text', 'partial_commentary', 'chinese_text', 'copyright_request', 'heart_censor', 'censored_nipples', 'page_number', 'scan', 'fake_magazine_cover', 'korean_commentary']

            if ',' in remove_tags:
                bad_tags.extend(remove_tags.split(','))
            else:
                bad_tags.append(remove_tags)

            if use_remove_txt:
                bad_tags.extend(open(os.path.join(user_remove_dir, choose_remove_txt), 'r').read().split(','))

            # Manage Backgrounds
            background_options = {
                'Add Background': ('detailed_background,' + random.choice(["outdoors", "indoors"]), COLORED_BG),
                'Remove Background': ('plain_background,simple_background,' + random.choice(COLORED_BG), ADD_BG),
                'Remove All': ('', COLORED_BG + ADD_BG)
            }

            if change_background in background_options:
                prompt_addition, tags_to_remove = background_options[change_background]
                bad_tags.extend(tags_to_remove)
                p.prompt = f'{p.prompt},{prompt_addition}' if p.prompt else prompt_addition

            # Manage Colors
            color_options = {
                'Colored': BW_BG,
                'Limited Palette': '(limited_palette:1.3)',
                'Monochrome': ','.join(BW_BG)
            }

            if change_color in color_options:
                color_option = color_options[change_color]
                if isinstance(color_option, list):
                    bad_tags.extend(color_option)
                else:
                    p.prompt = f'{p.prompt},{color_option}' if p.prompt else color_option

            if use_search_txt:
                search_tags = open(os.path.join(user_search_dir, choose_search_txt), 'r').read()
                search_tags_r = search_tags.replace(" ", "")
                split_tags = search_tags_r.splitlines()
                filtered_tags = [line for line in split_tags if line.strip()]
                rand_selected = random.randint(0, len(filtered_tags) - 1)
                selected_tags = filtered_tags[rand_selected]
                tags = f'{tags},{selected_tags}' if tags else selected_tags

            add_tags = '&tags=-animated'
            if tags:
                add_tags += f'+{tags.replace(",", "+")}'
                if mature_rating != 'All':
                    add_tags += f'+rating:{RATINGS[booru][mature_rating]}'

            # Getting Data
            random_post = {'preview_url': ''}
            prompts = []
            last_img = []
            preview_urls = []
            api_url = booru_apis.get(booru, Gelbooru(fringe_benefits))
            print(f'Using {booru}')

            # Secondary pool for preset-mix: image 2 onward drawn from a random
            # Tag Preset's search. Fetched BEFORE the primary pool on purpose:
            # get_data sets the global COUNT used by random_number for indexing,
            # and the primary pool's fetch must be the one that sets it last.
            preset_mix_data = None
            if mix_prompt and mix_from_presets and use_random_preset:
                preset_pool = [(tp, pp) for tp, pp in zip(
                    [tag_preset_1, tag_preset_2, tag_preset_3, tag_preset_4, tag_preset_5],
                    [preset_max_pages_1, preset_max_pages_2, preset_max_pages_3, preset_max_pages_4, preset_max_pages_5]
                ) if tp.strip()]
                if preset_pool:
                    mix_preset_tags, mix_preset_pages = random.choice(preset_pool)
                    mix_max_pages = int(mix_preset_pages) if mix_preset_pages and int(mix_preset_pages) > 0 else max_pages
                    mix_add_tags = '&tags=-animated' + f'+{mix_preset_tags.replace(",", "+")}'
                    if mature_rating != 'All':
                        mix_add_tags += f'+rating:{RATINGS[booru][mature_rating]}'
                    print(f'Ranbooru: preset-mix second pool: "{mix_preset_tags}" (max_pages={mix_max_pages})')
                    try:
                        preset_mix_data = api_url.get_data(mix_add_tags, mix_max_pages)
                        if not preset_mix_data.get('post'):
                            preset_mix_data = None
                            print('Ranbooru: preset-mix pool came back empty, mixing from primary pool instead')
                    except Exception:
                        preset_mix_data = None
                        print('Ranbooru: preset-mix fetch failed, mixing from primary pool instead')
                else:
                    print('Ranbooru: Mix from presets enabled but no presets defined')

            # Manage Post ID
            if post_id:
                data = api_url.get_post(add_tags, max_pages, post_id)
            else:
                data = api_url.get_data(add_tags, max_pages)

            # Normalise to a safe post list. A booru error page may return a dict
            # with no 'post' key, and an empty search returns an empty list; both
            # must NOT crash. When there are no posts we skip tag application
            # entirely but still fall through to LoRAnado below, leaving the
            # user's prompt untouched.
            posts = data.get('post') if isinstance(data, dict) else None
            if not posts:
                posts = []
            posts_found = len(posts) > 0
            data = {'post': posts}

            if not posts_found:
                print('Ranbooru: no posts found for this search - skipping tag '
                      'application (LoRAnado and the rest still run). '
                      'Try lowering Max Pages or changing the tags.')

            # Replace null scores with 0s
            for post in posts:
                post['score'] = post.get('score', 0)

            # Sort based on sorting_order
            if sorting_order == 'High Score':
                data['post'] = sorted(posts, key=lambda k: k.get('score', 0), reverse=True)
            elif sorting_order == 'Low Score':
                data['post'] = sorted(posts, key=lambda k: k.get('score', 0))
            posts = data['post']

            # Index range is the real page length, never the global COUNT.
            page_len = len(posts)
            if not posts_found:
                random_numbers = []
            elif post_id:
                print(f'Using post ID: {post_id}')
                random_numbers = [0 for _ in range(0, p.batch_size * p.n_iter)]
            else:
                random_numbers = self.random_number(sorting_order, p.batch_size * p.n_iter, count=page_len)
            for random_number in random_numbers:
                if same_prompt:
                    random_post = data['post'][random_numbers[0]]
                else:
                    if mix_prompt:
                        # Gather tag lists for mixing. Source 0 is THIS iteration's
                        # base post (random_number) so that the init image we later
                        # use (random_post.file_url) is one of the posts whose tags
                        # actually went into the mix - fixing the mismatch where the
                        # img2img seed image came from a post whose tags were unused.
                        # Sources 1+ come from the preset-mix pool when available,
                        # otherwise from further random posts in the primary pool.
                        source_tag_lists = []
                        for mix_i in range(0, mix_amount):
                            if mix_i == 0:
                                pool = data['post']
                                if not pool:
                                    continue
                                random_mix_number = random_number
                            elif preset_mix_data is not None and preset_mix_data.get('post'):
                                pool = preset_mix_data['post']
                                random_mix_number = random.randint(0, len(pool) - 1)
                            else:
                                pool = data['post']
                                if not pool:
                                    continue
                                if post_id:
                                    random_mix_number = random_number
                                else:
                                    picks = self.random_number(sorting_order, 1, count=len(pool))
                                    if not picks:
                                        continue
                                    random_mix_number = picks[0]
                            if random_mix_number >= len(pool):
                                continue
                            tag_list = [t for t in pool[random_mix_number]['tags'].split(' ') if t.strip()]
                            if tag_list:
                                source_tag_lists.append(tag_list)
                        # Distribute the tag budget across sources by mix_ratio:
                        # 0 = all from the first post, 1 = all from the last,
                        # 0.5 = even split. For 3+ posts, weights interpolate
                        # linearly from (1-ratio) to ratio across the sources.
                        mixed_tags = []
                        if source_tag_lists:
                            total_budget = max(len(tl) for tl in source_tag_lists)
                            n_sources = len(source_tag_lists)
                            if n_sources == 1:
                                weights = [1.0]
                            else:
                                raw = [(1.0 - mix_ratio) * (1 - i / (n_sources - 1)) + mix_ratio * (i / (n_sources - 1)) for i in range(n_sources)]
                                total_w = sum(raw)
                                weights = [w / total_w if total_w > 0 else 1.0 / n_sources for w in raw]
                            for tl, w in zip(source_tag_lists, weights):
                                take = min(len(tl), int(round(total_budget * w)))
                                if take > 0:
                                    mixed_tags.extend(random.sample(tl, take))
                        # De-duplicate while preserving order
                        seen_tags = set()
                        mixed_tags = [t for t in mixed_tags if not (t in seen_tags or seen_tags.add(t))]
                        # Copy the post so the cached data is never mutated
                        random_post = dict(data['post'][random_number])
                        if mixed_tags:
                            random_post['tags'] = ' '.join(mixed_tags)
                    else:
                        try:
                            random_post = data['post'][random_number]
                        except IndexError:
                            # Index landed past the fetched page. Skip this one
                            # rather than aborting the whole generation.
                            continue
                # Filter character tags (Gelbooru only) - only on the selected post
                post_tags = random_post['tags']
                if filter_character_tags and isinstance(api_url, Gelbooru):
                    post_tags = api_url.filter_character_tags(post_tags)
                clean_tags = post_tags.replace('(', r'\(').replace(')', r'\)')
                temp_tags = random.sample(clean_tags.split(' '), len(clean_tags.split(' '))) if shuffle_tags else clean_tags.split(' ')
                prompts.append(','.join(temp_tags))
                preview_urls.append(random_post.get('file_url', 'https://pic.re/image'))
                # Debug picture
                if DEBUG:
                    print(random_post)
            # Get Images (only when we actually selected posts)
            if posts_found and prompts and (use_img2img or use_deepbooru):
                image_urls = [preview_urls[-1]] if use_last_img else preview_urls

                for img in image_urls:
                    response = requests.get(img, headers=api_url.headers, timeout=REQUEST_TIMEOUT)
                    last_img.append(Image.open(BytesIO(response.content)))
            new_prompts = []
            # Cleaning Tags
            for prompt in prompts:
                prompt_tags = [tag for tag in html.unescape(prompt).split(',') if tag.strip() not in bad_tags]
                for bad_tag in bad_tags:
                    if '*' in bad_tag:
                        prompt_tags = [tag for tag in prompt_tags if bad_tag.replace('*', '') not in tag]
                new_prompt = ','.join(prompt_tags)
                if change_dash:
                    new_prompt = new_prompt.replace("_", " ")
                new_prompts.append(new_prompt)
            prompts = new_prompts

            # Drop fetched tags that already appear in the user's prompt.
            # Normalised comparison: case-insensitive, underscores and spaces
            # treated as equivalent, and any weight syntax like (tag:1.2) or
            # escaped parens stripped so 'blue_hair' matches '(blue hair:1.2)'.
            if skip_existing_tags and self.original_prompt:
                def _norm(tag):
                    t = tag.strip().lower()
                    t = t.replace('\\(', '').replace('\\)', '')
                    if t.startswith('(') and ':' in t:
                        t = t[1:].rsplit(':', 1)[0]
                    t = t.strip('()')
                    return t.replace('_', ' ').strip()

                existing = {_norm(t) for t in self.original_prompt.split(',') if _norm(t)}
                if existing:
                    deduped_prompts = []
                    total_removed = 0
                    for prompt in prompts:
                        kept = [t for t in prompt.split(',') if _norm(t) not in existing]
                        total_removed += len(prompt.split(',')) - len(kept)
                        deduped_prompts.append(','.join(kept))
                    prompts = deduped_prompts
                    if total_removed:
                        print(f'Ranbooru: skipped {total_removed} tag(s) already in the prompt')

            if not prompts:
                # No tags were applied (empty search). Leave p.prompt as the
                # user wrote it and fall through to LoRAnado / the rest.
                print('Ranbooru: no booru tags applied; keeping the original prompt.')
            elif len(prompts) == 1:
                print('Processing Single Prompt')
                p.prompt = f"{p.prompt},{prompts[-1]}" if p.prompt else prompts[-1]
                if chaos_mode in ['Chaos', 'Less Chaos']:
                    negative_prompt = '' if chaos_mode == 'Less Chaos' else p.negative_prompt
                    p.prompt, negative_prompt = generate_chaos(p.prompt, negative_prompt, chaos_amount)
                    p.negative_prompt = f"{p.negative_prompt},{negative_prompt}" if p.negative_prompt else negative_prompt
            else:
                print('Processing Multiple Prompts')
                negative_prompts = []
                new_prompts = []
                if chaos_mode == 'Chaos':
                    for prompt in prompts:
                        tmp_prompt, negative_prompt = generate_chaos(prompt, p.negative_prompt, chaos_amount)
                        new_prompts.append(tmp_prompt)
                        negative_prompts.append(negative_prompt)
                    prompts = new_prompts
                    p.negative_prompt = negative_prompts
                elif chaos_mode == 'Less Chaos':
                    for prompt in prompts:
                        tmp_prompt, negative_prompt = generate_chaos(prompt, '', chaos_amount)
                        new_prompts.append(tmp_prompt)
                        negative_prompts.append(negative_prompt)
                    prompts = new_prompts
                    p.negative_prompt = [p.negative_prompt + ',' + negative_prompt for negative_prompt in negative_prompts]
                else:
                    p.negative_prompt = [p.negative_prompt for _ in range(0, p.batch_size * p.n_iter)]
                p.prompt = prompts if not p.prompt else [f"{p.prompt},{prompt}" for prompt in prompts]
                if use_img2img:
                    if len(last_img) < p.batch_size * p.n_iter:
                        last_img = [last_img[0] for _ in range(0, p.batch_size * p.n_iter)]
            if negative_mode == 'Negative':
                # remove tags from p.prompt using tags from the original prompt
                orig_list = self.original_prompt.split(',')
                if isinstance(p.prompt, list):
                    new_positive_prompts = []
                    new_negative_prompts = []
                    for pr, npp in zip(p.prompt, p.negative_prompt):
                        clean_prompt = pr.split(',')
                        clean_prompt = [tag for tag in clean_prompt if tag not in orig_list]
                        clean_prompt = ','.join(clean_prompt)
                        new_positive_prompts.append(self.original_prompt)
                        new_negative_prompts.append(f'{npp},{clean_prompt}')
                    p.prompt = new_positive_prompts
                    p.negative_prompt = new_negative_prompts
                else:
                    clean_prompt = p.prompt.split(',')
                    clean_prompt = [tag for tag in clean_prompt if tag not in orig_list]
                    clean_prompt = ','.join(clean_prompt)
                    p.negative_prompt = f'{p.negative_prompt},{clean_prompt}'
                    p.prompt = self.original_prompt
            if negative_mode == 'Negative' or chaos_mode in ['Chaos', 'Less Chaos']:
                # NEGATIVE PROMPT FIX
                if model_hijack is not None:
                    neg_prompt_tokens = []
                    for pr in p.negative_prompt:
                        neg_prompt_tokens.append(model_hijack.get_prompt_lengths(pr)[1])
                    if len(set(neg_prompt_tokens)) != 1:
                        print('Padding negative prompts')
                        max_tokens = max(neg_prompt_tokens)
                        for num, neg in enumerate(neg_prompt_tokens):
                            while neg < max_tokens:
                                p.negative_prompt[num] += random.choice(p.negative_prompt[num].split(','))
                                neg = model_hijack.get_prompt_lengths(p.negative_prompt[num])[1]
                else:
                    print('Ranbooru: model_hijack unavailable in Forge Neo, skipping negative prompt padding.')

            if limit_tags < 1:
                if isinstance(p.prompt, list):
                    p.prompt = [limit_prompt_tags(pr, limit_tags, 'Limit') for pr in p.prompt]
                else:
                    p.prompt = limit_prompt_tags(p.prompt, limit_tags, 'Limit')

            if max_tags > 0:
                if isinstance(p.prompt, list):
                    p.prompt = [limit_prompt_tags(pr, max_tags, 'Max') for pr in p.prompt]
                else:
                    p.prompt = limit_prompt_tags(p.prompt, max_tags, 'Max')

            if use_same_seed:
                p.seed = random.randint(0, 2 ** 32 - 1) if p.seed == -1 else p.seed
                # One seed per generated image across the whole run, not just one
                # batch: a run produces batch_size * n_iter images.
                p.seed = [p.seed] * (p.batch_size * p.n_iter)

            # LORANADO
            p = self.loranado(lora_enabled, lora_folder, lora_amount, lora_min, lora_max, lora_custom_weights, p, lora_lock_prev, lora_folder_2_enabled, lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2)
            # Deepbooru only runs when the model is actually available. If it
            # isn't, skip the modify step entirely rather than letting the
            # fallback feed the bare original prompt into modify_prompt, which
            # in 'Replace' mode would discard the booru tags we just fetched.
            if use_deepbooru and not use_img2img and deepbooru is not None:
                self.last_img = last_img
                tagged_prompts = self.use_autotagger('deepbooru')
                # use_autotagger always returns a list (one entry per image).
                if not isinstance(tagged_prompts, list):
                    tagged_prompts = [tagged_prompts]

                if isinstance(p.prompt, list):
                    p.prompt = [
                        remove_repeated_tags(
                            modify_prompt(pr, tagged_prompts[num] if num < len(tagged_prompts) else tagged_prompts[-1], type_deepbooru)
                        )
                        for num, pr in enumerate(p.prompt)
                    ]
                else:
                    # Single prompt: pair with the first tagged result (a string),
                    # not the whole list, and don't index into the string itself.
                    tagged = tagged_prompts[0] if tagged_prompts else ''
                    p.prompt = remove_repeated_tags(modify_prompt(p.prompt, tagged, type_deepbooru))
            elif use_deepbooru and not use_img2img and deepbooru is None:
                print('Ranbooru: Deepbooru unavailable; keeping booru tags as-is.')

            if use_img2img:
                if not use_ip:
                    self.real_steps = p.steps
                    p.steps = 1
                    self.last_img = last_img
                if use_ip:
                    controlNetModule = importlib.import_module('extensions.sd-webui-controlnet.scripts.external_code', 'external_code')
                    controlNetList = controlNetModule.get_all_units_in_processing(p)
                    copied_network = controlNetList[0].__dict__.copy()
                    copied_network['enabled'] = True
                    copied_network['weight'] = denoising
                    array_img = np.array(last_img[0])
                    copied_network['image']['image'] = array_img
                    copied_networks = [copied_network] + controlNetList[1:]
                    controlNetModule.update_cn_script_in_processing(p, copied_networks)

        elif lora_enabled:
            p = self.loranado(lora_enabled, lora_folder, lora_amount, lora_min, lora_max, lora_custom_weights, p, lora_lock_prev, lora_folder_2_enabled, lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2)

    def postprocess(self, p, processed, enabled, tags, booru, remove_bad_tags, max_pages, change_dash, same_prompt, fringe_benefits, remove_tags, filter_character_tags, skip_existing_tags, use_img2img, denoising, use_last_img, change_background, change_color, shuffle_tags, post_id, mix_prompt, mix_amount, mix_ratio, mix_from_presets, chaos_mode, negative_mode, chaos_amount, limit_tags, max_tags, sorting_order, mature_rating, lora_folder, lora_amount, lora_min, lora_max, lora_enabled, lora_custom_weights, lora_lock_prev, lora_folder_2_enabled, lora_folder_2, lora_amount_2, lora_min_2, lora_max_2, lora_custom_weights_2, use_ip, use_search_txt, use_remove_txt, choose_search_txt, choose_remove_txt, search_refresh_btn, remove_refresh_btn, crop_center, use_deepbooru, type_deepbooru, use_same_seed, use_cache, api_key, user_id, save_credentials, credentials_status, clear_credentials_btn, use_random_preset=False, tag_preset_1='', tag_preset_2='', tag_preset_3='', tag_preset_4='', tag_preset_5='', preset_max_pages_1=0, preset_max_pages_2=0, preset_max_pages_3=0, preset_max_pages_4=0, preset_max_pages_5=0):
        if use_img2img and not use_ip and enabled:
            print('Using pictures')
            if crop_center:
                width, height = p.width, p.height
                self.last_img = [resize_image(img, width, height, cropping=True) for img in self.last_img]
            else:
                width, height = self.check_orientation(self.last_img[0])
            final_prompts = p.prompt
            if use_deepbooru:
                tagged_prompts = self.use_autotagger('deepbooru')
                if isinstance(p.prompt, list):
                    final_prompts = [modify_prompt(pr, tagged_prompts[num], type_deepbooru) for num, pr in enumerate(p.prompt)]
                    final_prompts = [remove_repeated_tags(pr) for pr in final_prompts]
                else:
                    final_prompts = modify_prompt(p.prompt, tagged_prompts, type_deepbooru)
                    final_prompts = remove_repeated_tags(final_prompts)
            p = StableDiffusionProcessingImg2Img(
                sd_model=shared.sd_model,
                outpath_samples=shared.opts.outdir_samples or shared.opts.outdir_img2img_samples,
                outpath_grids=shared.opts.outdir_grids or shared.opts.outdir_img2img_grids,
                prompt=final_prompts,
                negative_prompt=p.negative_prompt,
                seed=p.seed,
                sampler_name=p.sampler_name,
                scheduler=p.scheduler,
                batch_size=p.batch_size,
                n_iter=p.n_iter,
                steps=self.real_steps,
                cfg_scale=p.cfg_scale,
                width=width,
                height=height,
                init_images=self.last_img,
                denoising_strength=denoising,
            )
            proc = process_images(p)
            processed.images = proc.images
            processed.infotexts = proc.infotexts
            if use_last_img:
                processed.images.append(self.last_img[0])
            else:
                for num, img in enumerate(self.last_img):
                    processed.images.append(img)
                    processed.infotexts.append(proc.infotexts[num + 1])

    def random_number(self, sorting_order, size, count=None):
        """Generates random indices into the fetched page.

        Args:
            sorting_order (str): the sorting order. It can be 'Random', 'High Score' or 'Low Score'
            size (int): the amount of random numbers to generate
            count (int, optional): the number of posts actually available on the
                fetched page. When given, indices are drawn from this range rather
                than the global COUNT. For Gelbooru, COUNT is the total hit count
                (which can far exceed the posts on one page), so passing the real
                page length here prevents out-of-range indices. Falls back to the
                global COUNT when not supplied.

        Returns:
            list: the random indices (may be fewer than `size` if the page is small)
        """
        global COUNT
        n = count if count is not None else COUNT
        if n is None:
            n = 0
        # Never index past what the page actually holds.
        n = min(n, POST_AMOUNT)
        if n <= 0:
            return []
        # Sampling without replacement cannot return more than n distinct indices.
        draw = min(size, n)
        weights = np.arange(n, 0, -1)
        total = weights.sum()
        if sorting_order in ('High Score', 'Low Score') and total > 0:
            weights = weights / total
            random_numbers = np.random.choice(np.arange(n), size=draw, p=weights, replace=False)
        else:
            random_numbers = random.sample(range(n), draw)
        return list(random_numbers)

    def use_autotagger(self, model):
        """Use the autotagger to tag the images

        Args:
            model (str): the model to use. Right now only 'deepbooru' is supported

        Returns:
            list: the tagged prompts
        """
        if model == 'deepbooru':
            if deepbooru is None:
                print('Ranbooru: Deepbooru is not available in Forge Neo. Skipping autotagging.')
                return [self.original_prompt] if isinstance(self.original_prompt, str) else self.original_prompt
            if isinstance(self.original_prompt, str):
                orig_prompt = [self.original_prompt]
            else:
                orig_prompt = list(self.original_prompt)
            # Pair each image with a base prompt. When there are more images
            # than base prompts (a batch sharing one prompt), reuse the first.
            deepbooru.model.start()
            final_prompts = []
            for i, img in enumerate(self.last_img):
                base = orig_prompt[i] if i < len(orig_prompt) else orig_prompt[0]
                final_prompts.append(base + ',' + deepbooru.model.tag_multi(img))
            deepbooru.model.stop()
            return final_prompts
