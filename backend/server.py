# 必要なモジュールのインポート
from flask import Flask, request, jsonify, render_template, redirect
import MeCab, unidic_lite, re, hashlib
from google.cloud import vision, storage
import mysql.connector as mydb
from SPARQLWrapper import SPARQLWrapper, JSON
from datetime import datetime
import requests
import wikipedia
import subprocess
import mysql_maintenance
from PIL import Image
import io
import random

# Wikipediaの言語設定を日本語に設定
wikipedia.set_lang("ja")

# Flaskアプリケーションのセットアップ
app = Flask(__name__)

# Google Cloud Vision APIのクライアント設定
vision_client = vision.ImageAnnotatorClient.from_service_account_file(
    "/home/sdoi/LocaConne/locaconne.json"
)

# Cloud Storageのクライアント設定
storage_client = storage.Client.from_service_account_json(
    "/home/sdoi/LocaConne/locaconne.json"
)
bucket_name = "locaconne_bucket"

# SPARQLエンドポイントの設定
sparql = SPARQLWrapper("https://query.wikidata.org/sparql")

# MeCabの辞書にNEologdを指定
neologd_path = "/usr/lib/x86_64-linux-gnu/mecab/dic/mecab-ipadic-neologd"

# 除外ワードの設定
# 参考：https://qiita.com/yineleyici/items/73296d4b7491bdb77cd0
def WordVerification(text):
    """
    入力テキストが除外ワードに該当しないか確認する関数
    """
    # 除外ワードリスト
    exclusion_list = ["新開発", "日本", "関東", "関西", "東北", "東", "西", "南", "北"]
    
    # 除外ワードリストに含まれているかをチェック
    if text in exclusion_list:
        return False
    else:
        return True

# 地名だけ抽出する
def PlaceExtracting(text):
    """
    テキストから地名を抽出する関数
    """
    mecab = MeCab.Tagger(f'-d {neologd_path}')
    node = mecab.parseToNode(text)
    locList = []
    
    while node:
        pos = node.feature.split(",")
        
        # 固有名詞または一般名詞かつ除外ワードに引っかからない場合
        if WordVerification(node.surface):
            # 地域や観光地（温泉など）を抽出
            if (len(pos) > 1 and (pos[1] == "固有名詞" or pos[1] == "一般")):
                # 「温泉」などの特定キーワードが含まれる場合も抽出
                if pos[2] == "地域" or "温泉" in node.surface:
                    locList.append(node.surface)
        node = node.next

    return locList

# Wikidata APIを使用して地名から場所の情報を取得
def get_location_details(location):
    print(f"Searching for location: {location}")
    
    url = "https://www.wikidata.org/w/api.php"
    params = {
        'action': 'wbsearchentities',
        'search': location,
        'language': 'ja',
        'format': 'json',
        'limit': 1
    }
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        if 'search' in data and len(data['search']) > 0:
            qid = data['search'][0]['id']
            
            # SPARQLクエリで詳細情報を取得
            sparql.setQuery(f"""
                SELECT ?label ?description WHERE {{
                    wd:{qid} rdfs:label ?label .
                    OPTIONAL {{ wd:{qid} schema:description ?description . }}
                    FILTER (lang(?label) = "ja")
                    FILTER (lang(?description) = "ja")
                }}
                LIMIT 1
            """)
            sparql.setReturnFormat(JSON)
            results = sparql.query().convert()

            if results["results"]["bindings"]:
                label = results["results"]["bindings"][0]["label"]["value"]
                description = results["results"]["bindings"][0].get("description", {}).get("value", "")
            else:
                label = data['search'][0]['label']
                description = data['search'][0].get('description', '')
            
            # 座標を取得
            coordinate = get_coordinates_from_qid(qid)
            
            print(f"Found location: {label}, Description: {description}, Coordinate: {coordinate}")
            return label, description, coordinate
        else:
            print("No location found")
            return None, None, None

    except Exception as e:
        print(f"Error in get_location_details: {e}")
        return None, None, None

# QIDから座標情報を取得
def get_coordinates_from_qid(qid):
    sparql.setQuery(f"""
        SELECT ?coordinate WHERE {{
            wd:{qid} wdt:P625 ?coordinate.
        }}
        LIMIT 1
    """)
    sparql.setReturnFormat(JSON)
    
    try:
        results = sparql.query().convert()
        if results["results"]["bindings"]:
            return results["results"]["bindings"][0]["coordinate"]["value"]
    except Exception as e:
        print(f"Error in get_coordinates_from_qid: {e}")
    
    return None

# Wikipediaの概要を取得
def get_wikipedia_summary(place_label):
    try:
        summary = wikipedia.summary(place_label, sentences=2)
        return summary
    except wikipedia.exceptions.DisambiguationError as e:
        print(f"DisambiguationError: {e.options}")
        return None
    except wikipedia.exceptions.PageError:
        print(f"No Wikipedia page found for {place_label}")
        return None
    except Exception as e:
        print(f"Error getting Wikipedia summary for {place_label}: {e}")
        return None

# 地名の情報をlocation_detailsテーブルに保存する関数
def save_location_details(post_id, location):
    place_label, description, coordinate = get_location_details(location)
    if place_label:
        try:
            # Wikipediaの概要を取得
            wikipedia_summary = get_wikipedia_summary(place_label)
            conn = mysql_maintenance.create_mysql_connection()  # 接続を作成
            cursor = conn.cursor()
            sql = """
                INSERT INTO location_details (post_id, location, description, coordinate, wikipedia_summary)
                VALUES (%s, %s, %s, %s, %s)
            """
            val = (post_id, place_label, description, coordinate, wikipedia_summary)
            cursor.execute(sql, val)
            conn.commit()
            cursor.close()
            conn.close()  # 接続を切断
            print(f"Location details saved for post ID {post_id}")
        except mydb.Error as e:
            print(f"Error saving location details: {e}")

# 投稿フォーム表示用のエンドポイント
@app.route("/post-form", methods=["GET"])
def post_form():
    return render_template("post.html")

# 画像解析によるランドマーク検出
def detect_landmark(image_url):
    try:
        vision_image = vision.Image()
        vision_image.source.image_uri = image_url
        image_context = vision.ImageContext(language_hints=["ja"])
        response = vision_client.landmark_detection(
            image=vision_image, image_context=image_context
        )
        if not response.error.message and response.landmark_annotations:
            return response.landmark_annotations[0].description
    except Exception as e:
        print(f"Error in landmark detection: {e}")
    return None

def modify_image(image_url):
    """
    画像のサイズを変更する関数
    :param image_url: 画像のURL
    :param size: 新しいサイズ (幅, 高さ)
    :return: 新しい画像のURL
    """
    try:
        # 画像をダウンロード
        response = requests.get(image_url)
        response.raise_for_status()
        image_data = response.content

        # 画像を開く
        image = Image.open(io.BytesIO(image_data))
        
        original_width, original_height = image.size
        new_width = random.randint(640, original_width)
        new_height = random.randint(480, original_height)
        size = (new_width, new_height)

        # 画像のサイズを変更
        image = image.resize(size, Image.LANCZOS)

        # 新しいファイル名を生成
        new_filename = f"modified_{hashlib.sha256(image_data).hexdigest()}.jpg"

        # 変更した画像をCloud Storageにアップロード
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(new_filename)
        image_byte_array = io.BytesIO()
        image.save(image_byte_array, format='JPEG')
        blob.upload_from_string(image_byte_array.getvalue(), content_type='image/jpeg')

        # 新しい画像のURLを返す
        new_image_url = f"https://storage.googleapis.com/{bucket_name}/{new_filename}"
        return new_image_url

    except Exception as e:
        print(f"Error in modify_image: {e}")
        return None
    
# ユーザー投稿用のエンドポイント
@app.route("/post", methods=["POST"])
def post_content():
    data = request.form
    username = data.get("username", "")
    text = data.get("text", "")
    image = request.files.get("image")
    image_url = ""
    original_image_url = ""

    # 画像をCloud Storageにアップロード
    if image:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hash_input = f"{image.filename}_{current_time}"
        hashed_filename = hashlib.sha256(hash_input.encode()).hexdigest()

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(hashed_filename)
        blob.upload_from_file(image, content_type=image.content_type)
        image_url = f"https://storage.googleapis.com/{bucket_name}/{hashed_filename}"
        original_image_url = image_url  # オリジナルの画像URLを保存

    # 場所名の抽出（自然言語処理）
    locations = PlaceExtracting(text)
    print(locations)

    # 画像解析によるランドマーク検出を最大4回試行
    landmark = None
    for attempt in range(10):
        if image_url:
            landmark = detect_landmark(image_url)
            if landmark:
                break
            # 画像の一部を変更する処理を追加（例：サイズ変更）
            image_url = modify_image(image_url)

    # データベースに投稿を保存
    conn = mysql_maintenance.create_mysql_connection()  # 接続を作成
    cursor = conn.cursor()
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sql = "INSERT INTO posts (username, text, image_url, time) VALUES (%s, %s, %s, %s)"
    val = (username, text, original_image_url, current_time)  # オリジナルの画像URLを保存
    cursor.execute(sql, val)
    conn.commit()
    post_id = cursor.lastrowid  # 保存した投稿のIDを取得
    cursor.close()
    conn.close()  # 接続を切断

    # 地名情報を取得して保存
    selected_location = None
    if landmark and locations:
        # 両方が存在する場合は画像からのランドマークを使用
        selected_location = landmark
    elif locations:
        # テキストからの地名のみが存在する場合
        selected_location = locations[0]  # 最初の地名を使用
    elif landmark:
        # 画像からのランドマークのみが存在する場合
        selected_location = landmark

    if selected_location:
        save_location_details(post_id, selected_location)
    else:
        print("No location information available to save.")

    return jsonify({"status": "success"})

# タイムライン表示用のエンドポイント
@app.route("/timeline", methods=["GET"])
def get_timeline():
    conn = mysql_maintenance.create_mysql_connection()  # 接続を作成
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT p.id, p.username, p.text, p.image_url, p.time, ld.wikipedia_summary
        FROM posts p
        LEFT JOIN location_details ld ON p.id = ld.post_id
        ORDER BY p.time DESC
        """
    )
    posts = cursor.fetchall()
    cursor.close()
    conn.close()  # 接続を切断
    return render_template("timeline.html", posts=posts)

@app.route('/post/<int:post_id>', methods=['GET'])
def post_details(post_id):
    conn = mysql_maintenance.create_mysql_connection()  # 接続を作成
    cursor = conn.cursor(dictionary=True)
    # 投稿内容を取得
    cursor.execute("SELECT * FROM posts WHERE id = %s", (post_id,))
    post = cursor.fetchone()
    # 場所の詳細を取得
    cursor.execute("""
        SELECT description, coordinate, wikipedia_summary
        FROM location_details WHERE post_id = %s
    """, (post_id,))
    location = cursor.fetchone()
    cursor.close()
    conn.close()  # 接続を切断

    description = location['description'] if location else None
    coordinate = location['coordinate'] if location else None
    wikipedia_summary = location['wikipedia_summary'] if location else None
    return render_template(
        'details.html',
        post=post,
        description=description,
        coordinate=coordinate,
        wikipedia_summary=wikipedia_summary
    )

# リダイレクト
@app.route("/")
def main():
    return redirect("/timeline")

# メイン関数
if __name__ == "__main__":
    # Flaskアプリケーションをホスト0.0.0.0でポート5000で起動
    app.run(host='0.0.0.0', port=5000, debug=True)