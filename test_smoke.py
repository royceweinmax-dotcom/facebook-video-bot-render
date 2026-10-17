from bot import normalize_facebook_url, choose_options

assert normalize_facebook_url("https://www.facebook.com/watch/?v=123")
assert normalize_facebook_url("https://fb.watch/abc123/")
assert normalize_facebook_url("https://example.com/video") is None

result = choose_options({
    "title": "Test",
    "thumbnail": "https://example.com/a.jpg",
    "formats": [
        {"url": "https://example.com/sd.mp4", "height": 480, "vcodec": "h264", "acodec": "aac", "ext": "mp4"},
        {"url": "https://example.com/hd.mp4", "height": 1080, "vcodec": "h264", "acodec": "aac", "ext": "mp4"},
    ],
})
assert result.hd and result.hd.height == 1080
assert result.sd and result.sd.height == 480
print("smoke ok")
