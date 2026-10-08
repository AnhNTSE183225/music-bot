with open('config.yaml', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('    stop:\n      mode: "open"', '    stop:\n      mode: "open"\n\n    pause:\n      mode: "admin_only"\n\n    resume:\n      mode: "admin_only"')

with open('config.yaml', 'w', encoding='utf-8') as f:
    f.write(content)
