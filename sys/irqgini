#!/usr/bin/env python3
import re

with open('/proc/interrupts') as f:
    lines = f.readlines()

n_cpus = len(lines[0].split())
counts = [0] * n_cpus

for line in lines[1:]:
    if re.search('nvme', line, re.IGNORECASE):
        parts = line.split()
        for i in range(n_cpus):
            try:
                counts[i] += int(parts[i + 1])
            except (IndexError, ValueError):
                pass

total = sum(counts)
if not total:
    print('No NVMe interrupts counted')
    raise SystemExit(1)

shares = sorted([c / total for c in counts], reverse=True)
n = len(shares)
s = sorted(shares)
gini = 1 - 2 * sum((n - i) * x for i, x in enumerate(s)) / (n * sum(s))

print(f'Total NVMe interrupts : {total:,}')
print(f'CPUs                  : {n}')
print(f'Top 1 core share      : {shares[0]*100:.1f}%')
print(f'Top 2 cores share     : {sum(shares[:2])*100:.1f}%')
print(f'Top 4 cores share     : {sum(shares[:4])*100:.1f}%')
print(f'Gini                  : {gini:.3f}  (0=even, 1=one core takes all)')
