import json
import os
import io
import base64
import boto3
import requests
import pandas as pd
from PIL import Image
from geopy.geocoders import Nominatim
from github import Github
from botocore.exceptions import ClientError
from xml.etree import ElementTree
from math import ceil, sqrt
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

# Initialize AWS clients
s3_client = boto3.client('s3')

# Initialize Nominatim geolocator
geolocator = Nominatim(user_agent="water_resource_geocoder")

def estimate_size_per_pixel(img, test_size=1000):
    """Estimate size per pixel using a sample."""
    w, h = img.size
    test_w = min(test_size, w)
    test_h = min(test_size, h)
    small_part = img.crop((0, 0, test_w, test_h)).convert('RGB')
    img_byte_arr = io.BytesIO()
    small_part.save(img_byte_arr, format='PNG')
    S_small = len(img_byte_arr.getvalue())
    P_small = test_w * test_h
    return S_small / P_small if P_small > 0 else 0

def split_image(image_path, overlap=0.1, max_size=10 * 1024 * 1024, base_upscale_factor=1.0, min_grid_size=4):
    """Split image into parts that are within size limits."""
    img = Image.open(image_path)
    width, height = img.size

    size_per_pixel = estimate_size_per_pixel(img, test_size=5000)
    if size_per_pixel == 0:
        print("Image is too small to estimate size per pixel.")
        return [img]

    max_dim = 8000
    overlap_factor = 1 + 2 * overlap
    safety_factor = 0.7

    M = max(min_grid_size, ceil(width * overlap_factor / max_dim))
    N = max(min_grid_size, ceil(height * overlap_factor / max_dim))

    upscale_factor = base_upscale_factor
    while True:
        grid_width = width / M
        grid_height = height / N
        overlap_width = grid_width * overlap
        overlap_height = grid_height * overlap
        image_parts = []

        all_parts_within_limit = True
        for i in range(M):
            for j in range(N):
                left = max(0, i * grid_width - overlap_width)
                upper = max(0, j * grid_height - overlap_height)
                right = min(width, (i + 1) * grid_width + overlap_width)
                lower = min(height, (j + 1) * grid_height + overlap_height)
                part = img.crop((left, upper, right, lower))
                new_width = int((right - left) * upscale_factor)
                new_height = int((lower - upper) * upscale_factor)
                part = part.resize((new_width, new_height), Image.LANCZOS)
                img_byte_arr = io.BytesIO()
                part.save(img_byte_arr, format='PNG')
                part_size = len(img_byte_arr.getvalue())
                if part_size > max_size * safety_factor:
                    all_parts_within_limit = False
                    break
                image_parts.append(part)
            if not all_parts_within_limit:
                break

        if all_parts_within_limit:
            return image_parts
        else:
            oversize_factor = part_size / (max_size * safety_factor)
            area_reduction_factor = sqrt(oversize_factor)
            M = int(M * area_reduction_factor) + 1
            N = int(N * area_reduction_factor) + 1

        if grid_width < 500 or grid_height < 500:
            if upscale_factor > 1.0:
                upscale_factor -= 0.5
                M = max(min_grid_size, ceil(width * overlap_factor / max_dim))
                N = max(min_grid_size, ceil(height * overlap_factor / max_dim))
            else:
                return image_parts

def ocr_image_parts(image_parts):
    """Perform OCR on image parts using Textract."""
    ocr_results = []
    for idx, part in enumerate(image_parts):
        part = part.convert('RGB')
        img_byte_arr = io.BytesIO()
        part.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()

        img_size = len(img_bytes)
        if img_size > 10000000:
            print(f"Warning: Part {idx} exceeds Textract size limit (10 MB)")
            continue

        try:
            Image.open(io.BytesIO(img_bytes)).verify()
        except Exception as e:
            print(f"Error verifying part {idx}: {e}")
            continue

        try:
            response = textract_client.detect_document_text(Document={'Bytes': img_bytes})
            text_blocks = [block['Text'] for block in response['Blocks'] if block['BlockType'] == 'LINE' and block['Confidence'] > 70]
            ocr_results.append({'part': idx, 'text': text_blocks})
        except textract_client.exceptions.InvalidParameterException as e:
            continue
        except Exception as e:
            print(f"Unexpected error for part {idx}: {e}")
            continue
    return ocr_results

def combine_ocr_results(ocr_results):
    """Combine OCR results, removing duplicates."""
    combined_text = []
    seen_text = set()
    for result in ocr_results:
        for text in result['text']:
            if text not in seen_text:
                seen_text.add(text)
                combined_text.append(text)
    return " ".join(combined_text)

def analyze_with_bedrock(ocr_text, bedrock_model_id):
    prompt = (
        "From the following text, extracted from a 1956 map of Colorado's water resources, "
        "extract all names of water bodies (rivers, lakes, creeks, streams) and water infrastructure "
        "(reservoirs, dams, canals, ditches, power plants related to water management). "
        "Include only existing features. Exclude towns, mountains, proposed or other non-water features. "
        "Ensure that you capture the full name of each water resource, including identifiers like 'Lake', 'Reservoir', or 'Canal'. "
        "For example, if the text mentions 'Jackson Lake', return 'Jackson Lake', not just 'Jackson'. "
        "The text originally included township-range values (e.g., 'T6N', 'R 76W'), which have been removed. "
        "Be cautious with word combinations; for example, 'River Michigan Creek Deer' might be 'River Michigan' and 'Deer Creek'. "
        "Return only the list of names, separated by commas, in title case (e.g., 'River Michigan'), "
        "without additional text or explanation. "
        "Text: {ocr_text}"
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 10000,
        "top_k": 0,
        "stop_sequences": [],
        "temperature": 0,
        "top_p": 0,
        "messages": [{"role": "user", "content": prompt.format(ocr_text=ocr_text)}]
    }).encode('utf-8')
    try:
        response = bedrock_client.invoke_model(
            modelId= bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=body
        )
        result = json.loads(response['body'].read().decode('utf-8'))
        return result.get('content', [{}])[0].get('text', '')
    except Exception as e:
        print(f"Bedrock error: {e}")
        return "Error analyzing text with Bedrock"


def upload_to_github(repo_name, file_path, content, commit_message):
    """Upload or update a file in a GitHub repository."""
    try:
        repo = github_client.get_user().get_repo(repo_name)
        try:
            contents = repo.get_contents(file_path)
            repo.update_file(file_path, commit_message, content, contents.sha)
        except Exception:
            repo.create_file(file_path, commit_message, content)
        return f"https://github.com/{github_client.get_user().login}/{repo_name}/blob/main/{file_path}"
    except Exception as e:
        print(f"Error uploading to GitHub: {e}")
        raise e

def lambda_handler(event, context):
    bucket_name = os.environ.get("BUCKET_NAME")
    error_folder = os.environ.get("ERROR_FOLDER", "error")
    analysis_folder = os.environ.get("ANALYSIS_FOLDER", "analysis")
    github_token = os.environ.get("GITHUB_TOKEN")
    github_repo_name = os.environ.get("GITHUB_REPO_NAME")
    bedrock_model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-7-sonnet-20250219-v1:0")
    bedrock_region = os.environ.get("BEDROCK_REGION", "us-west-2")

    global textract_client
    textract_client = boto3.client('textract', region_name=bedrock_region)

    global bedrock_client
    bedrock_client = boto3.client("bedrock-runtime", region_name=bedrock_region)

    global github_client
    github_client = Github(github_token)
        
    try:
        
        # Step 1: Extract S3 event details
        record = event['Records'][0]
        s3_info = record['s3']
        event_bucket = s3_info['bucket']['name']
        object_key = s3_info['object']['key']
        image_name = os.path.basename(object_key)
        local_image_path = f"/tmp/{image_name}"

        if event_bucket != bucket_name:
            error_message = f"Event bucket {event_bucket} does not match configured BUCKET_NAME {bucket_name}"
            return {
                "statusCode": 400,
                "body": json.dumps({"error": error_message})
            }

        s3_client.download_file(bucket_name, object_key, local_image_path)

        # Step 2: Split image into parts
        image_parts = split_image(local_image_path,overlap=0.1, min_grid_size=6)

        # Step 3: Perform OCR with Textract
        ocr_results = ocr_image_parts(image_parts)
        combined_text = combine_ocr_results(ocr_results)

        # Step 4: Extract water resources with Bedrock
        n_runs = 3
        all_names = set()
        for run in range(n_runs):
            water_resource_names = analyze_with_bedrock(combined_text, bedrock_model_id)
            if water_resource_names.startswith("Error"):
                print(water_resource_names)
                break
            names_list = [name.strip().lower() for name in water_resource_names.split(',')]
            all_names.update(names_list)

        # Final unique names in title case
        water_resources = [name.title() for name in sorted(all_names)]

        # Step 5: Process coordinates for water resources
        resource_coords = {}
        for name in water_resources:
            coord = None
            coord_source = ""
            if not coord:
                try:
                    location = geolocator.geocode(f"{name}, Colorado, USA", timeout=2)
                    if location:
                        coord = {'latitude': location.latitude, 'longitude': location.longitude}
                        coord_source = f"Geocoded: {name}, Colorado, USA"
                    else:
                        print(f"{name}: No Township-Range, Could not geocode")
                except (GeocoderTimedOut, GeocoderServiceError) as e:
                    print(f"{name}: Geocoding error: {e}")
                    # Retry once with a delay
                    try:
                        time.sleep(1)  # Wait 2 seconds before retrying
                        location = geolocator.geocode(
                            f"{name}, Colorado, USA", timeout=2
                        )
                        if location:
                            coord = {'latitude': location.latitude, 'longitude': location.longitude}
                            coord_source = f"Geocoded: {name}, Colorado, USA"
                        else:
                            print(f"{name}: Retry failed, could not geocode")
                    except (GeocoderTimedOut, GeocoderServiceError) as e:
                        print(f"{name}: Retry geocoding error: {e}")

            if coord:
                resource_coords[name] = coord

        # Step 6: Compute bounding box
        print("Computing bounding box")
        if resource_coords:
            lats = [coord['latitude'] for coord in resource_coords.values()]
            lons = [coord['longitude'] for coord in resource_coords.values()]
            west = min(lons)
            east = max(lons)
            north = max(lats)
            south = min(lats)
            center_lat = (north + south) / 2
            center_lon = (west + east) / 2
            bbox_polygon = [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south]
            ]
            bounding_box_str = f"ENVELOPE({west},{east},{north},{south})"
            bounding_box_source = "Derived from Water Resource Coordinates"
        else:
            center_lat = ""
            center_lon = ""
            bbox_polygon = []
            bounding_box_str = "No coordinates available"
            bounding_box_source = "No coordinates available"

        # Step 8: Build GeoJSON
        print("Building GeoJSON")
        features = []
        if bbox_polygon:
            bbox_feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [bbox_polygon]
                },
                "properties": {
                    "name": "Map Boundary",
                    "source": "Derived from Water Resource Coordinates"
                }
            }
            features.append(bbox_feature)

        for name, coord in resource_coords.items():
            point_feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [coord['longitude'], coord['latitude']]
                },
                "properties": {
                    "name": name,
                    "coordinate_source": "Geocoded"
                }
            }
            features.append(point_feature)

        geojson_data = {
            "type": "FeatureCollection",
            "features": features
        }

        # Step 8: Upload GeoJSON to GitHub
        geojson_file_name = f"{os.path.splitext(image_name)[0]}.geojson"
        geojson_url = upload_to_github(
            github_repo_name,
            geojson_file_name,
            json.dumps(geojson_data, indent=2),
            f"Add GeoJSON for {image_name}"
        )

        # Step 10: Generate and upload Excel sheet
        map_description = "Map of water resources in Colorado, 1956"  # Placeholder
        county_str = "Unknown"  # Placeholder
        spatial_coverage = "; ".join(water_resources) if water_resources else ""
        description_csv = ("This item includes: " + ", ".join(water_resources) + ".") if water_resources else ""
        
        csv_row = {
            "Title*": os.path.splitext(image_name)[0],
            "Alternate Title": "",
            "Creator*": "",
            "Contributor": "",
            "Artist": "",
            "Author": "",
            "Composer": "",
            "Editor": "",
            "Lyricist": "",
            "Producer": "",
            "Publisher": "",
            "Coverage": map_description,
            "Spatial Coverage": spatial_coverage,
            "Temporal Coverage": "",
            "Latitude": center_lat,
            "Longitude": center_lon,
            "Bounding Box": bounding_box_str,
            "External Reference": geojson_url,
            "Advisor": "",
            "Committee Member": "",
            "Degree Name": "",
            "Degree Level": "",
            "Department": "",
            "University": "",
            "Date*": "",
            "Date Created": "",
            "Date Issued": "",
            "Date Recorded": "",
            "Date Submitted": "",
            "Date Search*": "",
            "Description": description_csv,
            "Abstract": "",
            "Award": "",
            "Frequency": "",
            "Sponsorship": "",
            "Table of Contents": "",
            "Subject": "",
            "LCSH Subject*": "",
            "Language": "",
            "Language-ISO": "",
            "Format": "",
            "Medium*": "",
            "Extent": "",
            "Type*": "",
            "Source": "",
            "Digital Collection*": "",
            "Physical Collection*": "",
            "Series/Location*": "",
            "Subcollection": "",
            "Repository*": "",
            "Rights*": "",
            "Rights Note": "",
            "Rights License": "",
            "Rights URI": "",
            "Rights DPLA*": "",
            "Identifier": "",
            "Citation": "",
            "DOI": "",
            "ISBN": "",
            "URI": "",
            "Related Resource*": "",
            "Relation-Has Format Of": "",
            "Relation-Has Part": "",
            "Relation-Has Version": "",
            "Relation-Is Format Of": "",
            "Relation-Is Referenced By": "",
            "Relation-Is Replaced By": "",
            "Relation-Is Version Of": "",
            "Relation-References": "",
            "Relation-Replaces": "",
            "Transcript": "",
            "Path": "",
            "File Name": image_name
        }

        # Step 11: Append or create CSV file in S3
        analysis_csv_key = f"{analysis_folder}/dublin core metadata analysis file.csv"
        try:
            existing_obj = s3_client.get_object(Bucket=bucket_name, Key=analysis_csv_key)
            existing_csv = existing_obj["Body"].read().decode("utf-8")
            df_existing = pd.read_csv(io.StringIO(existing_csv))
            df_new = pd.DataFrame([csv_row])
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        except s3_client.exceptions.NoSuchKey:
            df_combined = pd.DataFrame([csv_row])
        except Exception as e:
            print(f"Error reading existing CSV: {str(e)}. Creating new CSV")
            df_combined = pd.DataFrame([csv_row])

        csv_buffer = io.StringIO()
        df_combined.to_csv(csv_buffer, index=False)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=analysis_csv_key,
            Body=csv_buffer.getvalue(),
            ContentType='text/csv'
        )

        # Step 12: Return success response
        response = {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Processing completed successfully",
                "geojson_url": geojson_url,
                "csv_location": f"s3://{bucket_name}/{analysis_csv_key}"
            })
        }
        print("Lambda execution completed successfully")
        return response

    except Exception as e:
        error_message = f"Error processing image '{object_key}': {str(e)}"
        print(error_message)
        error_file_name = f"{os.path.splitext(image_name)[0]}.txt"
        s3_client.put_object(
            Bucket=bucket_name,
            Key=f"{error_folder}/{error_file_name}",
            Body=error_message
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"error": error_message})
        }