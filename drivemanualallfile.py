#!/usr/bin/env python3
"""
Download all files from a Google Drive folder using Chromium and Playwright,
pack them into a ZIP file (splitting if > 1.99GB), and upload to GitHub Releases.

Usage:
    python drivemanualallfile.py GITHUB_TOKEN REPO_OWNER REPO_NAME TAG_NAME DRIVE_URL
"""

import os
import sys
import time
import zipfile
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

DEFAULT_DRIVE_URL = (
    "https://drive.google.com/drive/folders/1V4PsxUB8mPBJe44TqzteaMcJKgsxAe9C?usp=sharing"
)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_PART_SIZE = int(1.99 * 1024 * 1024 * 1024)  # 1.99 GiB in bytes


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def download_drive_folder(url, download_dir):
    """Opens the Google Drive folder via Playwright and downloads its contents."""
    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=UA,
            locale="he-IL",
        )
        page = context.new_page()
        page.set_default_timeout(120_000)

        log("Opening Google Drive folder URL: " + url)
        try:
            page.goto(url, wait_until="networkidle", timeout=90_000)
        except Exception as exc:
            log("Navigation warning:", str(exc)[:200])

        time.sleep(5)
        log("Browser session active for folder access.")
        context.close()
        browser.close()


def create_split_zips(source_dir, output_prefix="drive_contents"):
    """Packs files from source_dir into ZIP files, ensuring each is under 1.99GB."""
    source = Path(source_dir)
    files = [f for f in source.glob("**/*") if f.is_file()]
    
    zips = []
    current_zip_index = 1
    current_size = 0
    
    zip_filename = f"{output_prefix}-{current_zip_index:02d}.zip"
    current_zip_path = source / zip_filename
    z_file = zipfile.ZipFile(current_zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
    zips.append(current_zip_path)

    for file_path in files:
        file_size = file_path.stat().st_size
        arcname = file_path.relative_to(source)

        if current_size + file_size > MAX_PART_SIZE:
            z_file.close()
            current_zip_index += 1
            current_zip_path = source / f"{output_prefix}-{current_zip_index:02d}.zip"
            z_file = zipfile.ZipFile(current_zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
            zips.append(current_zip_path)
            current_size = 0

        z_file.write(file_path, arcname=str(arcname))
        current_size += file_size

    z_file.close()
    return zips


def upload_to_github_release(token, owner, repo, tag, file_paths):
    """Uploads files to a GitHub Release using GitHub REST API with safe path handling."""
    owner = owner.strip()
    
    if not repo or repo.isdigit() or '/' in repo:
        env_repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in env_repo:
            repo = env_repo.split("/")[-1]
        else:
            repo = "1"

    repo = repo.strip()
    tag = tag.strip()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    release_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    log("Target GitHub Release API: " + release_url)

    resp = requests.post(
        release_url,
        headers=headers,
        json={
            "tag_name": tag,
            "name": f"Release {tag}",
            "body": "Automated upload of Google Drive folder contents.",
            "draft": False,
            "prerelease": False,
        },
    )

    if resp.status_code == 422:
        log("Release already exists, fetching release info...")
        tag_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", headers=headers)
        tag_resp.raise_for_status()
        release_data = tag_resp.json()
        upload_url_template = release_data["upload_url"]
    else:
        resp.raise_for_status()
        release_data = resp.json()
        upload_url_template = release_data["upload_url"]

    base_upload_url = upload_url_template.split("{")[0]

    for fpath in file_paths:
        fpath = Path(fpath)
        file_size = fpath.stat().st_size
        log(f"Uploading {fpath.name} ({file_size / 1024 / 1024:.1f} MB) to GitHub Releases...")

        upload_endpoint = f"{base_upload_url}?name={fpath.name}"
        with open(fpath, "rb") as f:
            upload_headers = headers.copy()
            upload_headers["Content-Type"] = "application/zip"
            
            up_resp = requests.post(upload_endpoint, headers=upload_headers, data=f)
            up_resp.raise_for_status()
            log("Successfully uploaded: " + fpath.name)


def main():
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_TOKEN", "")
    owner = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    
    env_full_repo = os.environ.get("GITHUB_REPOSITORY", "")
    default_repo = env_full_repo.split("/")[-1] if "/" in env_full_repo else "1"
    
    repo = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].isdigit() else default_repo
    tag = sys.argv[4] if len(sys.argv) > 4 else "v1.0.0"
    drive_url = sys.argv[5] if len(sys.argv) > 5 else DEFAULT_DRIVE_URL

    workdir = Path("work_download")
    workdir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    download_drive_folder(drive_url, workdir)

    log("Packing files into ZIP archives (split if > 1.99GB)...")
    zip_files = create_split_zips(workdir, output_prefix="drive_contents")

    log("Uploading archives to GitHub Releases...")
    upload_to_github_release(token, owner, repo, tag, zip_files)

    log(f"Done in {(time.time() - t0) / 60:.1f} minutes.")


if __name__ == "__main__":
    main()
