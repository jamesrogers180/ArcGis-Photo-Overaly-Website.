# This is a sample Python script.
import os
import time
from io import BytesIO

import PIL.ImageFont as ImageFont
from PIL import Image, ImageDraw
from arcgis.features import FeatureLayer
from arcgis.gis import GIS
from flask import Flask, request, render_template, redirect, url_for, send_file, session, flash, g, jsonify
from flask_executor import Executor
import zipfile
import json
from threading import Thread
import shutil


app = Flask(__name__)
executor = Executor(app)
app.config['UPLOAD_FOLDER'] = os.path.abspath('uploads')
app.config['PROCESSED_IMAGES_FOLDER'] = os.environ.get('PROCESSED_IMAGES_FOLDER')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'secret_key_for_development')

gis = None
fl = None
remaining_images = 0
processing_timestamp = None

# ...

@app.route('/', methods=['GET', 'POST'])
def index():
    global gis
    if request.method == 'POST':
        # Get input values from the form
        login_name = request.form['login_name']
        password = request.form['password']
        layer_url = request.form['layer_url']
        if not layer_url.endswith('/0'):
            layer_url += '/0'
        try:
            sign_in_result, error = arc_sign_in(login_name, password)
            if error:
                flash(error, category='error')
                return render_template('index.html')
            gis = sign_in_result
            session['layer_url'] = layer_url
            return redirect(url_for('process_checkboxes'))
        except Exception as e:
            flash(f"An unexpected error occurred: {e}", category='error')
            return render_template('index.html')
    return render_template('index.html')


def arc_sign_in(login_name, password):
    try:
        gis = GIS('https://msugis.maps.arcgis.com', login_name, password)
        return gis, None
    except Exception as e:
        return None, f"Error: Unable to sign in. Please check your login credentials. ({e})"


@app.route('/checkboxes', methods=['GET', 'POST'])
def checkboxes():
    global gis, layer_url
    layer_url = session.get('layer_url')
    fields, fl = get_fields(layer_url, gis)


    return render_template('checkboxes.html', fields=fields)


@app.route('/process_checkboxes', methods=['GET', 'POST'])
def process_checkboxes():
    global gis, layer_url, remaining_images, processing_timestamp
    layer_url = session.get('layer_url')
    fields, fl = get_fields(layer_url, gis)
    layer_name = fl.properties['name']
    total_features = get_total_features(layer_url, gis)
    if request.method == 'POST':
        if layer_url:
            selected_fields = request.form.getlist('field_checkbox')
            if 'process_all' in request.form:  # Check if the "Process All" button was clicked
                start_object_id = 1
                end_object_id = total_features
            else:
                try:
                    start_object_id = int(request.form['start_object_id'])
                    end_object_id = int(request.form['end_object_id'])
                except ValueError:
                    flash("Error: Start and End Object IDs must be valid integers.", category='error')
                    return render_template('checkboxes.html', fields=fields, layer_name=layer_name, total_features=total_features)


            # Get the survey results and attachments
            survey_results, attachment_list = make_lists(fl)


            #set a range of object id's to allow for a select few to be processed and not all of them at once
            attachment_list = [att for att in attachment_list if start_object_id <= att['objectid'] <= end_object_id]
            # counter for how many images are bing processed
            remaining_images = len(attachment_list)
            # Create a folder for the processed images
            timestamp = int(time.time())
            processing_timestamp = timestamp  # Store the timestamp in the global variable
            folder = os.path.join(app.config['UPLOAD_FOLDER'], f"processed_images_{timestamp}")
            os.makedirs(folder)

            # Start image processing in a separate thread
            processing_thread = Thread(target=process_images,
                                       args=(attachment_list, folder, fl, survey_results, selected_fields))
            processing_thread.start()

            return redirect(url_for('processing'))

        # Add this return statement to render the checkboxes template for a 'GET' request
    return render_template('checkboxes.html', fields=fields, layer_name=layer_name, total_features=total_features)


@app.route('/processed_images/<timestamp>', methods=['GET', 'POST'])
def processed_images(timestamp):
    if request.method == 'POST':
        # Create a ZIP file containing the processed images
        zip_filename = f'processed_images_{timestamp}.zip'
        zip_path = os.path.join(app.config['UPLOAD_FOLDER'], zip_filename)

        processed_images = get_processed_images(timestamp)
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for image_path in processed_images:
                # Calculate the relative path to preserve the folder structure
                rel_path = os.path.relpath(image_path,
                                           os.path.join(app.config['UPLOAD_FOLDER'], f"processed_images_{timestamp}"))
                zipf.write(image_path, rel_path)

        # Send the ZIP file as a downloadable attachment
        response =  send_file(zip_path, as_attachment=True)
        remove_folder(os.path.join(app.config['UPLOAD_FOLDER'], f"processed_images_{timestamp}"))
        os.remove(zip_path)

        return response

    else:
        # Render the processed_images.html template
        return render_template('processed_images.html', timestamp=timestamp)


def process_images(attachment_list, folder, fl, survey_results, selected_fields, max_downloads=10):
    global remaining_images
    downloaded_paths = download_attachments(attachment_list, folder, fl)  # , max_downloads
    if not downloaded_paths:
        return None
    print('download complete')
    add_text_to_images(downloaded_paths, survey_results, selected_fields, attachment_list, folder)
    print('you have finished adding text and are returning processed_paths')

    return folder  # Return the folder path instead of redirecting


# layer_url = "https://services.arcgis.com/uHAHKfH1Z5ye1Oe0/arcgis/rest/services/survey123_713482a1049948678f4eb8a2a19b9abc/FeatureServer/0"


def download_attachments(attachments, folder, fl):  # , max_downloads=10):
    save_paths = []
    for i, att in enumerate(attachments):  # [:max_downloads]
        object_id = att['objectid']
        attachment_id = att['id']
        file_name = f'object_id_{object_id}'
        save_path = os.path.join(folder, file_name)
        print(f"Save path: {save_path}")  # Add this line to print the save_path
        try:
            attachment_data = fl.attachments.download(object_id, attachment_id, save_path)
        except Exception as e:
            print(f"Error downloading attachment with object ID {object_id}. Skipping... ({e})")
            continue
        print(f"Downloaded attachment for object ID {object_id}")
        save_path = os.path.join(folder, file_name, att['name'])
        save_paths.append((object_id, attachment_id, file_name, save_path))
    return save_paths


def add_text_to_images(image_paths, survey_results, selected_fields, attachments, folder):
    global remaining_images
    processed_image_paths = []
    print(selected_fields)
    for i, path in enumerate(image_paths):
        print(path)
        attachment_obj_id = attachments[i]['objectid']
        print(f"Processing object ID {attachment_obj_id}")
        print(attachment_obj_id)
        survey_result = next((r for r in survey_results if r.attributes['objectid'] == attachment_obj_id), None)
        if survey_result is None:
            print(f"No survey result found for attachment with object ID {attachment_obj_id}. Skipping...")
            continue
        for r in survey_results:
            if attachment_obj_id == r.attributes['objectid']:
                # Check if the transect and site name match
                with open(path[3], 'rb') as f:
                    img_data = f.read()
                    img = Image.open(BytesIO(img_data))
                    exif_data = img.getexif()
                    orientation = exif_data.get(274, 1) if exif_data else 1
                    if orientation == 3:
                        img = img.rotate(180, expand=True)
                    elif orientation == 6:
                        img = img.rotate(270, expand=True)
                    elif orientation == 8:
                        img = img.rotate(90, expand=True)
                    draw = ImageDraw.Draw(img)
                    font = ImageFont.truetype('Times New Roman.ttf', size=30)
                    attribute_values = ", ".join(
                        [f"{field}: {survey_result.attributes[field]}" for field in selected_fields])
                    text = attribute_values
                    print(f"Adding text: {text}")
                    position = (10, 10)
                    text_bbox = draw.textbbox(position, text, font=font)
                    draw.rectangle(text_bbox, fill='black')
                    draw.text(position, text, font=font, fill='white')
                    # img = img.transpose(method=Image.TRANSPOSE)
                    img.save(path[3])
                    print(f"Added text to image {path[3]}")
                    remaining_images -= 1


def get_fields(layer_url, gis):
    global fl
    fl = FeatureLayer(layer_url, gis)
    return [field['name'] for field in fl.properties.fields], fl


def make_lists(fl):
    survey_results = []

    attachment_list = []
    for feature in fl.query().features:
        obj_id = feature.attributes['objectid']
        attachments = fl.attachments.get_list(obj_id)
        for attachment in attachments:
            attachment['objectid'] = obj_id
        attachment_list.extend(attachments)
        survey_results.append(feature)
    return survey_results, attachment_list


def get_processed_images(timestamp):
    folder = os.path.join(app.config['UPLOAD_FOLDER'], f"processed_images_{timestamp}")
    if not os.path.exists(folder):
        return []

    processed_images = []
    for root, dirs, files in os.walk(folder):
        for filename in files:
            file_path = os.path.join(root, filename)
            if os.path.isfile(file_path) and os.path.splitext(file_path)[1].lower() in ['.jpg', '.jpeg', '.png',
                                                                                        '.gif']:
                processed_images.append(file_path)

    return processed_images


@app.route('/processing', methods=['GET'])
def processing():
    return render_template('processing.html')


@app.route('/check_status')
def check_status():
    global remaining_images, processing_timestamp
    print(remaining_images)
    if remaining_images <= 0:
        return jsonify({'status': 'ready', 'timestamp': processing_timestamp})
    else:
        return jsonify({'status': 'processing', 'remaining': remaining_images})

def get_total_features(layer_url, gis):
    fl = FeatureLayer(layer_url, gis)
    return fl.query(return_count_only=True)

def remove_folder(folder_path):
    if os.path.exists(folder_path) and os.path.isdir(folder_path):
        try:
            shutil.rmtree(folder_path)
        except Exception as e:
            print(f"Error removing folder '{folder_path}': {e}")

if __name__ == '__main__':
    app.run(debug=True)
