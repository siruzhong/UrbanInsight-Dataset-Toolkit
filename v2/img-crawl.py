import csv
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image
from PIL import ImageDraw
from loguru import logger
from shapely import Polygon
from shapely.geometry import box
from shapely.wkt import loads

# Access key for Baidu Maps
# Configuration viewable at: https://lbsyun.baidu.com/apiconsole/center#/home
ak = '1ZtwxRT5sUDd6jaj0c7sCpjy9zXTl10O'


def preprocess(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        file_content = file.read()

    # 将NBSP替换为空格
    file_content = file_content.replace('\xa0', ' ')

    # 将替换后的内容写回文件
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write(file_content)


# Convert latitude and longitude to Baidu map coordinates
def bd_latlng2xy(zoom, latitude, longitude):
    url = "https://api.map.baidu.com/geoconv/v1/"
    # For detailed parameters, refer to https://lbs.baidu.com/faq/api?title=webapi/guide/changeposition-base
    params = {
        "coords": str(longitude) + ',' + str(latitude),
        "from": "5",
        "to": "6",
        "ak": ak,
    }
    response = requests.get(url=url, params=params)
    result = response.json()
    loc = result["result"][0]
    res = 2 ** (18 - zoom)  # Calculate the scaling factor
    x = loc['x'] / res
    y = loc['y'] / res
    return x, y


def bd_xy2latlng(zoom, x, y):
    """Convert BD09 pixel coordinates to WGS84 lat/lng for a given zoom level."""
    res = 2 ** (18 - zoom)
    bd_x = x * 256 * res
    bd_y = y * 256 * res

    params = {
        "coords": f"{bd_y},{bd_x}",
        "from": "6",  # BD09
        "to": "5",  # WGS84
        "ak": ak
    }
    response = requests.get(url="https://api.map.baidu.com/geoconv/v1/", params=params)
    response.raise_for_status()  # Raise an exception for HTTP errors
    loc = response.json()["result"][0]
    return loc['y'], loc['x']  # latitude, longitude


# 高德坐标转百度（传入经度、纬度）
def convert_gd_to_baidu(gg_lng, gg_lat):
    X_PI = math.pi * 3000.0 / 180.0
    x = gg_lng
    y = gg_lat
    z = math.sqrt(x * x + y * y) + 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) + 0.000003 * math.cos(x * X_PI)
    bd_lng = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return {
        "bd_lat": bd_lat,
        "bd_lng": bd_lng
    }


# Download map tiles
def download_tiles(city, zoom, latitude_start, latitude_stop, longitude_start, longitude_stop, satellite=True):
    # Create a save directory with a separate subdirectory for each city
    root_save = os.path.join("img/tiles", city)
    os.makedirs(root_save, exist_ok=True)

    # Perform coordinate conversion
    start_x, start_y = bd_latlng2xy(zoom, latitude_start, longitude_start)
    stop_x, stop_y = bd_latlng2xy(zoom, latitude_stop, longitude_stop)

    # Calculate tile range
    start_x = int(start_x // 256)
    start_y = int(start_y // 256)
    stop_x = int(stop_x // 256) + 1  # Make sure it is at least 1 greater than start_x
    stop_y = int(stop_y // 256) + 1  # Make sure it is at least 1 greater than start_y

    # 计算网格尺寸
    grid_size_x = stop_x - start_x
    grid_size_y = stop_y - start_y
    logger.info(f'x range: {start_x} to {stop_x}')
    logger.info(f'y range: {start_y} to {stop_y}')

    # Loop to download each tile, using a thread pool of custom size, e.g., max_workers=666
    tile_paths = []
    with ThreadPoolExecutor(max_workers=666) as executor:
        futures = []
        for x in range(start_x, stop_x):
            for y in range(start_y, stop_y):
                tile_path = os.path.join(root_save, f"{zoom}_{x}_{y}_s.jpg")
                tile_paths.append(tile_path)
                futures.append(executor.submit(download_tile, x, y, zoom, satellite, root_save))
        # Wait for all threads to complete
        for future in futures:
            future.result()

    # Return tile paths, top-left tile coordinates, and grid size
    return tile_paths, (start_x, stop_y), (grid_size_x, grid_size_y)


# Download an individual map tile
def download_tile(x, y, zoom, satellite, root_save):
    if satellite:
        # Satellite imagery URL
        url = f"http://shangetu0.map.bdimg.com/it/u=x={x};y={y};z={zoom};v=009;type=sate&fm=46&udt=20150504&app=webearth2&v=009&udt=20150601"
        filename = f"{zoom}_{x}_{y}_s.jpg"
    else:
        # Road map image URL
        url = f'http://online3.map.bdimg.com/tile/?qt=tile&x={x}&y={y}&z={zoom}&styles=pl&scaler=1&udt=20180810'
        filename = f"{zoom}_{x}_{y}_r.png"

    filename = os.path.join(root_save, filename)

    # Check if the file exists, download if it doesn't
    if not os.path.exists(filename):
        try:
            logger.info(f'downloading filename: {filename}')
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            logger.info(f"-- saving {filename}")
            with open(filename, 'wb') as f:
                f.write(response.content)
            time.sleep(random.random())  # Random sleep to reduce server load
        except requests.RequestException as e:
            logger.info(f"-- {filename} -> {e}")
    else:
        logger.info(f"File already exists: {filename}")


def parse_aoi_file(file_path):
    aois = []
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            wkt = row['wkt']
            # 转换WKT中的坐标点
            polygon = loads(wkt)
            converted_polygon = []
            for point in polygon.exterior.coords:
                lat, lon = point[1], point[0]
                conversion_result = convert_gd_to_baidu(lon, lat)
                converted_lat = conversion_result["bd_lat"]
                converted_lon = conversion_result["bd_lng"]
                converted_polygon.append((converted_lon, converted_lat))
            # 创建转换后的多边形
            converted_polygon = Polygon(converted_polygon)
            aoi = {
                'address': row['aoi_address'],
                'centroid': row['centroid'],
                'polygon': converted_polygon,
                'bounding_square': get_bounding_square(converted_polygon)
            }
            aois.append(aoi)
    return aois


def get_bounding_square(polygon):
    minx, miny, maxx, maxy = polygon.bounds
    return box(minx, miny, maxx, maxy)


def stitch_tiles(tile_paths, grid_size):
    if not tile_paths:
        logger.info("No tiles to stitch")
        return

    tile_width, tile_height = Image.open(tile_paths[0]).size
    total_width = tile_width * grid_size[0]
    total_height = tile_height * grid_size[1]
    stitched_image = Image.new('RGB', (total_width, total_height))

    # 根据文件名中的x和y坐标对瓦片路径进行排序
    # 首先提取 x 和 y 的值，然后按 y 升序、x 升序排列
    tile_paths.sort(key=lambda path: (int(path.split('_')[2]), int(path.split('_')[1])))

    for i, tile_path in enumerate(tile_paths):
        with Image.open(tile_path) as tile:
            # 计算瓦片在拼接图像中的位置
            x_index = i % grid_size[0]
            y_index = i // grid_size[0]
            x = x_index * tile_width
            y = (grid_size[1] - 1 - y_index) * tile_height  # 从底部开始计算 y 坐标
            stitched_image.paste(tile, (x, y))

    return stitched_image


def apply_mask(stitched_image, aoi_polygon, tile_bounds, save_path):
    # 将AOI多边形的地理坐标转换为图像上的像素坐标
    def convert_coords(coords):
        min_lon, min_lat, max_lon, max_lat = tile_bounds
        x_percent = (coords[0] - min_lon) / (max_lon - min_lon)
        y_percent = (max_lat - coords[1]) / (max_lat - min_lat)
        return (x_percent * stitched_image.width, y_percent * stitched_image.height)

    # 确定AOI多边形在拼接图像上的像素坐标
    aoi_coords = [convert_coords(point) for point in aoi_polygon.exterior.coords]

    # 获取AOI多边形的像素边界
    aoi_pixel_bounds = [(min(x[0] for x in aoi_coords), min(y[1] for y in aoi_coords)),
                        (max(x[0] for x in aoi_coords), max(y[1] for y in aoi_coords))]

    # 裁剪拼接图像以获取AOI覆盖的矩形区域
    crop_box = (aoi_pixel_bounds[0][0], aoi_pixel_bounds[0][1],
                aoi_pixel_bounds[1][0], aoi_pixel_bounds[1][1])
    logger.info(f"Cropping with box: {crop_box}")
    aoi_image = stitched_image.crop(crop_box)

    # 在AOI覆盖的矩形图像上创建遮罩
    mask = Image.new('L', aoi_image.size, 0)
    draw = ImageDraw.Draw(mask)
    # 转换AOI坐标为相对于矩形图像的坐标
    relative_aoi_coords = [(x[0] - aoi_pixel_bounds[0][0], x[1] - aoi_pixel_bounds[0][1]) for x in aoi_coords]
    # 在遮罩上绘制多边形
    draw.polygon(relative_aoi_coords, fill=255)
    black_background = Image.new('RGB', aoi_image.size)
    # 应用遮罩
    masked_aoi_image = Image.composite(aoi_image, black_background, mask)
    # 保存遮罩后的AOI图像
    masked_aoi_image.save(save_path)

    logger.info(f"Masked image saved to {save_path}")


def crop_stitched_image(stitched_image, start_lat, start_lon, stop_lat, stop_lon, zoom, top_left_x_tile,
                        top_left_y_tile):
    # Convert the start and stop coordinates to pixel coordinates
    start_x, start_y = bd_latlng2xy(zoom, start_lat, start_lon)
    stop_x, stop_y = bd_latlng2xy(zoom, stop_lat, stop_lon)

    # Calculate the pixel coordinates of the start and stop points relative to the entire stitched image
    start_x_rel = int(start_x - top_left_x_tile * 256)
    start_y_rel = -int(start_y - top_left_y_tile * 256)
    stop_x_rel = int(stop_x - top_left_x_tile * 256)
    stop_y_rel = -int(stop_y - top_left_y_tile * 256)

    # 确保裁剪区域坐标正确
    left = min(start_x_rel, stop_x_rel)
    upper = min(start_y_rel, stop_y_rel)
    right = max(start_x_rel, stop_x_rel)
    lower = max(start_y_rel, stop_y_rel)

    # 确保坐标在图像范围内
    left = max(0, left)
    upper = max(0, upper)
    right = min(stitched_image.width, right)
    lower = min(stitched_image.height, lower)

    # Crop the stitched image
    crop_area = (left, upper, right, lower)
    cropped_image = stitched_image.crop(crop_area)
    return cropped_image


def main():
    preprocess('aoi.csv')
    aois = parse_aoi_file('aoi.csv')
    zoom = 19  # Coarse zoom level
    satellite = True  # Satellite image

    for aoi in aois:
        square = aoi['bounding_square']
        lon_start, lat_start, lon_stop, lat_stop = square.bounds
        tile_paths, (top_left_x_tile, top_left_y_tile), grid_size = \
            download_tiles(aoi['address'], zoom, lat_start, lat_stop, lon_start, lon_stop, satellite)

        # Stitch tiles and save the stitched image
        stitched_image = stitch_tiles(tile_paths, grid_size)
        if stitched_image:
            # Crop the stitched image based on the original coordinates
            cropped_image = crop_stitched_image(
                stitched_image, lat_start, lon_start, lat_stop, lon_stop, zoom, top_left_x_tile, top_left_y_tile)
            cropped_save_path = os.path.join("img/cropped_images", f"{aoi['address']}.jpg")
            os.makedirs(os.path.dirname(cropped_save_path), exist_ok=True)
            cropped_image.save(cropped_save_path)
            logger.info(f"Cropped image saved to {cropped_save_path}")

            stitched_sava_path = os.path.join("img/stitched_images", f"{aoi['address']}.jpg")
            os.makedirs(os.path.dirname(stitched_sava_path), exist_ok=True)
            stitched_image.save(stitched_sava_path)
            logger.info(f"Stitched image saved to {stitched_sava_path}")

            # Apply the mask based on AOI polygon and save the final image
            tile_bounds = (lon_start, lat_start, lon_stop, lat_stop)
            masked_sava_path = os.path.join("img/masked_images", f"{aoi['address']}.jpg")
            os.makedirs(os.path.dirname(masked_sava_path), exist_ok=True)
            apply_mask(cropped_image, aoi['polygon'], tile_bounds, masked_sava_path)
            logger.info(f"Masked image saved to {masked_sava_path}")


if __name__ == "__main__":
    main()
