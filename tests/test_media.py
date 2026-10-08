#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入图加载门禁 —— 并行下载的**保序**与**报错确定性**。

并行代码最容易出的两类错都在这里钉住：
  ① 结果顺序被"谁先完成"带跑（垫图顺序会影响生成语义）；
  ② 报错随线程调度漂移（同一个请求两次得到不同的错误，没法排查）。
"""
from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from app import media
from app.errors import InvalidParameterError
from app.media import load_images


def _png(rgb: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), rgb).save(buf, "PNG")
    return buf.getvalue()


def _stub_download(delays: dict[str, float], bad: set[str] | None = None):
    """按 URL 决定耗时/是否坏图的下载替身（不碰网络）。

    🔴 2026-10-03：返回对象额外带**并发观测**（`peak` = 同时在飞的峰值）。
    这样"是否并发下载"可以用**计数**断言，而不是掐墙钟 ——
    计时断言在负载高的机器上会假红，且放宽门限后又失去鉴别力
    （详见 `test_multi_url_download_is_parallel` 的说明）。
    """
    bad = bad or set()

    class _Stub:
        inflight = 0
        peak = 0

        def __call__(self, url: str, _settings) -> bytes:
            _Stub.inflight += 1
            _Stub.peak = max(_Stub.peak, _Stub.inflight)
            try:
                time.sleep(delays.get(url, 0.01))
                return b"not-an-image" if url in bad else _png()
            finally:
                _Stub.inflight -= 1

    return _Stub()


def test_multi_url_download_is_parallel(monkeypatch, settings):
    """多张 URL **并发**下载 —— 用**计数**判定，不掐墙钟。

    🔴 2026-10-03 改判据：原先断言 `elapsed < 0.14s`（后放宽到 0.5s），
    **已失去鉴别力** —— 串行下界 0.10s、并发在负载高时也能到 0.5s，
    两者落进同一区间 ⇒ 真退化成串行也照样绿。
    （实测：用 `git stash` 对照基线，该断言在全量跑时同样会红
    ⇒ 它测的是**机器状态**而不是代码。）

    ✅ 现在断言 `stub.peak >= 2`：只要**同时有两个**下载在飞就是并发。
    **与机器负载无关**；退化成串行时峰值必然 = 1 ⇒ 必红。
    """
    urls = ["https://a/1.png", "https://a/2.png", "https://a/3.png"]
    # 让**第一个**最慢 ⇒ 串行必超时；并发则接近 0.06s
    stub = _stub_download({urls[0]: 0.06, urls[1]: 0.02, urls[2]: 0.02})
    monkeypatch.setattr(media, "download", stub)

    t0 = time.time()
    blobs = load_images(urls, settings)
    elapsed = time.time() - t0

    assert len(blobs) == 3
    assert stub.peak >= 2, (
        f"峰值并发只有 {stub.peak} ⇒ **串行下载**"
        f"（墙钟 {elapsed:.2f}s 仅供参考，不再是判据）")
    assert stub.inflight == 0, "结束后不该还有在飞的下载"



def test_multi_url_download_keeps_input_order(monkeypatch, settings):
    """🔴 **保序**：返回的 Blob 顺序必须与传入的 URL 顺序一致。

    垫图的先后对生成语义有影响（见 `build_blend_draft`）。
    这里刻意让"完成顺序"与"提交顺序"相反（越靠前越慢），
    所以"谁先下完谁排前面"的实现会在这条翻车。
    """
    urls = ["https://a/slow.png", "https://a/mid.png", "https://a/fast.png"]
    monkeypatch.setattr(media, "download", _stub_download(
        {urls[0]: 0.06, urls[1]: 0.03, urls[2]: 0.005}))

    blobs = load_images(urls, settings)

    assert [b.src for b in blobs] == urls, "顺序被完成顺序带跑了"


def test_multi_url_download_error_is_deterministic(monkeypatch, settings):
    """报错要**按输入顺序**遇到第一个坏的 —— 不随线程调度漂移。

    否则同一个请求两次可能得到不同的错误信息（指向不同的图），根本没法排查。
    `Executor.map` 按输入顺序产出，所以第一条坏图必定先报。
    """
    urls = ["https://a/ok.png", "https://a/bad1.png", "https://a/bad2.png"]
    monkeypatch.setattr(media, "download", _stub_download(
        {urls[0]: 0.005, urls[1]: 0.05, urls[2]: 0.01}, bad={urls[1], urls[2]}))

    with pytest.raises(InvalidParameterError) as ei:
        load_images(urls, settings)

    assert "magic bytes" in str(ei.value)


def test_single_ref_takes_the_sync_path(monkeypatch, settings):
    """单张不该为一次调用付线程池的钱（也顺带覆盖最常见的路径）。"""
    calls: list[str] = []
    monkeypatch.setattr(media, "download",
                        lambda url, _s: (calls.append(url), _png())[1])

    blobs = load_images(["https://a/only.png"], settings)

    assert [b.src for b in blobs] == ["https://a/only.png"]
    assert calls == ["https://a/only.png"]


def test_empty_refs_is_empty(settings):
    assert load_images([], settings) == []


# ---------------------------------------------------------------------------
# 复用上游资产（链式任务：拿上一次的产物当这次输入）
# ---------------------------------------------------------------------------

#: 实测的真实产物 URL 形态（bucket 与上传产出的 key **同一形态**）
_REAL = ("https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/"
         "27a0e40f9075488582c4021b19fad34c~tplv-tb4s082cfz-aigc_resize:0:0.png"
         "?lk3s=7c3bb0db&x-expires=1789765200&x-signature=x")


@pytest.mark.parametrize("url,expected", [
    (_REAL, "tos-cn-i-tb4s082cfz/27a0e40f9075488582c4021b19fad34c"),
    # 裸对象地址（没有 ~ 变换后缀）也要认
    ("https://p11-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/deadbeefdeadbeef",
     "tos-cn-i-tb4s082cfz/deadbeefdeadbeef"),
])
def test_reuse_extracts_the_upstream_asset_uri(url, expected):
    assert media.reuse_image_uri(url) == expected


@pytest.mark.parametrize("url", [
    # 🔴 换个 host 一律不认 —— 否则任意站点上一个恰好长这样的路径都会被当成我们的资产，
    # 于是拿着一个不存在的 key 去建任务。
    "https://evil.example.com/tos-cn-i-tb4s082cfz/27a0e40f9075488582c4021b19fad34c",
    "https://byteimg.com.evil.com/tos-cn-i-tb4s082cfz/27a0e40f9075488582c4021b19fad34c",
    # key 不是 hex ⇒ 不认（挡住路径穿越式的怪值）
    "https://p26-dreamina-sign.byteimg.com/tos-cn-i-x/../../etc/passwd",
    # 普通外链 / data URI / 空 ⇒ 不认（走原来的下载+上传）
    "https://cdn.example.com/cat.png",
    "data:image/png;base64,AAAA",
    "",
])
def test_reuse_refuses_anything_that_is_not_an_upstream_asset(url):
    assert media.reuse_image_uri(url) is None
