#!/usr/bin/env python3
"""
publish.py — 将 Obsidian 笔记发布到 Hugo 博客

用法：
  python3 publish.py "网络/虚拟机网络拓扑.md"
  python3 publish.py "kubernetes/Docker.md" --no-push   # 只本地生成，不推送

说明：
  - 自动将 ![[xxx.excalidraw | size]] 转换为 SVG 图片引用
  - 自动将 ![[xxx.png]] 转换为标准 Markdown 图片引用
  - 图片复制到 static/images/，笔记复制到 content/posts/
  - 完成后自动 git commit & push
"""

import os
import re
import shutil
import sys
import subprocess
from pathlib import Path

VAULT = Path("/Users/wubuwei/Documents/Obsidian Vault")
BLOG = Path("/Users/wubuwei/Desktop/blog")
IMAGES_DIR = BLOG / "static" / "images"
POSTS_DIR = BLOG / "content" / "posts"


def find_in_vault(filename: str):
    """在 vault 中递归查找文件名匹配的文件（忽略路径）"""
    for path in VAULT.rglob(filename):
        # 跳过 .obsidian 目录
        if ".obsidian" not in path.parts:
            return path
    return None


def process_note(note_rel_path: str, push: bool = True):
    note_path = VAULT / note_rel_path
    if not note_path.exists():
        print(f"❌ 找不到笔记：{note_path}")
        sys.exit(1)

    content = note_path.read_text(encoding="utf-8")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)

    # 匹配 ![[filename]] 或 ![[filename | size]]
    embed_pattern = re.compile(r'!\[\[([^\]|]+?)(?:\s*\|\s*[\w\d]+)?\]\]')
    warnings = []

    def replace_embed(match):
        raw = match.group(1).strip()

        # Excalidraw：![[xxx.excalidraw | 600]]
        if raw.endswith(".excalidraw"):
            svg_name = raw + ".svg"
            svg_path = find_in_vault(svg_name)
            if svg_path:
                dest = IMAGES_DIR / svg_name
                shutil.copy2(svg_path, dest)
                print(f"  ✅ 复制 SVG：{svg_name}")
                return f"![{raw}](/images/{svg_name})"
            else:
                warnings.append(
                    f"⚠️  未找到 SVG：{svg_name}\n"
                    f"   请在 Obsidian Excalidraw 插件设置中开启 Auto-export SVG，\n"
                    f"   然后打开该图并保存一次（Cmd+S）。"
                )
                return match.group(0)  # 保留原始语法，等 SVG 生成后重新发布

        # 普通图片：![[xxx.png]] / ![[xxx.jpg]] 等
        img_path = find_in_vault(raw)
        if img_path:
            dest = IMAGES_DIR / raw
            shutil.copy2(img_path, dest)
            print(f"  ✅ 复制图片：{raw}")
            return f"![{raw}](/images/{raw})"
        else:
            warnings.append(f"⚠️  未找到图片：{raw}")
            return match.group(0)

    new_content = embed_pattern.sub(replace_embed, content)

    # 写出处理后的笔记
    post_name = note_path.stem + ".md"
    post_path = POSTS_DIR / post_name
    post_path.write_text(new_content, encoding="utf-8")
    print(f"  ✅ 笔记已写入：content/posts/{post_name}")

    # 打印警告
    for w in warnings:
        print(f"\n{w}")

    if warnings:
        print("\n⚠️  存在未处理的内容，请解决后重新运行脚本。")
        if push:
            answer = input("是否仍然继续推送？[y/N] ").strip().lower()
            if answer != "y":
                print("已取消推送。")
                return

    # Git commit & push
    os.chdir(BLOG)
    subprocess.run(["git", "add", "."], check=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True
    )
    if result.returncode == 0:
        print("\n没有需要提交的变更。")
        return

    commit_msg = f"发布：{note_path.stem}"
    subprocess.run([
        "git",
        "-c", "user.name=litturtleee",
        "-c", "user.email=litturtleee@gmail.com",
        "commit", "-m", commit_msg
    ], check=True)

    if push:
        subprocess.run(["git", "push"], check=True)
        print(f"\n🚀 已推送！GitHub Actions 将自动构建部署，约 1-2 分钟后生效。")
    else:
        print(f"\n✅ 提交完成（未推送，使用 git push 手动推送）。")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    note_arg = sys.argv[1]
    do_push = "--no-push" not in sys.argv

    print(f"\n📝 正在发布：{note_arg}\n")
    process_note(note_arg, push=do_push)
