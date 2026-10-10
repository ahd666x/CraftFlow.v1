import io

path = 'storefront/models.py'
data = open(path, encoding='utf-8').read()

idx = data.find('regulation')
print('idx', idx)
print('context:', repr(data[idx-40:idx+80]))

# Remove the stray English word that was accidentally prepended
data = data.replace("' regulation settings سایت'", "' regulation settings سایت'")

open(path, 'w', encoding='utf-8').write(data)
print('cleaned')