from app.public_markdown import render_public_markdown


def test_supported_markdown_is_rendered():
    rendered = str(render_public_markdown(
        "# Operator\n\n"
        "A **bold** and *careful* note with [privacy](https://example.org/privacy).\n\n"
        "- First item\n"
        "- Second item\n\n"
        "1. One\n"
        "2. Two\n\n"
        "[Email](mailto:privacy@example.org)"
    ))

    assert "<h2>Operator</h2>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<em>careful</em>" in rendered
    assert '<a href="https://example.org/privacy"' in rendered
    assert '<a href="mailto:privacy@example.org"' in rendered
    assert "<ul><li>First item</li><li>Second item</li></ul>" in rendered
    assert "<ol><li>One</li><li>Two</li></ol>" in rendered


def test_html_images_and_unsafe_links_never_become_active_content():
    rendered = str(render_public_markdown(
        '<script>alert("x")</script>\n\n'
        '<iframe src="https://evil.example"></iframe>\n\n'
        '![tracking](https://evil.example/pixel.png)\n\n'
        '[script](javascript:alert(1))\n\n'
        '[data](data:text/html,evil)\n\n'
        '[relative](/admin)'
    ))

    assert "<script" not in rendered
    assert "<iframe" not in rendered
    assert "<img" not in rendered
    assert "javascript:" in rendered
    assert "data:text/html" in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="data:' not in rendered
    assert 'href="/admin"' not in rendered
    assert "&lt;script&gt;" in rendered
    assert "![tracking]" in rendered


def test_link_attributes_and_labels_are_escaped():
    rendered = str(render_public_markdown(
        '[<b>Label</b>](https://example.org/?q="quoted")'
    ))

    assert "&lt;b&gt;Label&lt;/b&gt;" in rendered
    assert 'q=&quot;quoted&quot;' in rendered
    assert 'rel="noopener noreferrer"' in rendered
