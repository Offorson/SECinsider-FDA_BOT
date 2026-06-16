import cairosvg

svg_a = '''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000" viewBox="0 0 1000 1000">
 <defs>
  <radialGradient id="bg" cx="50%" cy="42%" r="78%">
   <stop offset="0%" stop-color="#123E5E"/>
   <stop offset="58%" stop-color="#0A2438"/>
   <stop offset="100%" stop-color="#04111C"/>
  </radialGradient>
  <linearGradient id="sweep" x1="0.5" y1="0" x2="1" y2="0.55">
   <stop offset="0%" stop-color="#2BE08C" stop-opacity="0.55"/>
   <stop offset="100%" stop-color="#2BE08C" stop-opacity="0"/>
  </linearGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
   <stop offset="0%" stop-color="#AFFFD8" stop-opacity="0.95"/>
   <stop offset="32%" stop-color="#2BE08C" stop-opacity="0.6"/>
   <stop offset="100%" stop-color="#2BE08C" stop-opacity="0"/>
  </radialGradient>
 </defs>
 <rect width="1000" height="1000" fill="url(#bg)"/>
 <g fill="none" stroke="#2BE08C">
  <circle cx="500" cy="500" r="410" stroke-opacity="0.30" stroke-width="4"/>
  <circle cx="500" cy="500" r="300" stroke-opacity="0.45" stroke-width="5"/>
  <circle cx="500" cy="500" r="180" stroke-opacity="0.65" stroke-width="6"/>
 </g>
 <g stroke="#2BE08C" stroke-opacity="0.14" stroke-width="3">
  <line x1="90" y1="500" x2="910" y2="500"/>
  <line x1="500" y1="90" x2="500" y2="910"/>
 </g>
 <path d="M500,500 L500,90 A410,410 0 0 1 790,210 Z" fill="url(#sweep)"/>
 <line x1="500" y1="500" x2="500" y2="90" stroke="#2BE08C" stroke-opacity="0.5" stroke-width="3"/>
 <polyline points="320,650 430,560 540,600 670,360" fill="none" stroke="#2BE08C" stroke-width="16" stroke-linejoin="round" stroke-linecap="round"/>
 <g fill="#0A2438" stroke="#2BE08C" stroke-width="6">
  <circle cx="320" cy="650" r="10"/>
  <circle cx="430" cy="560" r="10"/>
  <circle cx="540" cy="600" r="10"/>
 </g>
 <circle cx="670" cy="360" r="120" fill="url(#glow)"/>
 <circle cx="670" cy="360" r="30" fill="#EAFFF3"/>
 <circle cx="670" cy="360" r="30" fill="none" stroke="#2BE08C" stroke-width="6"/>
</svg>'''

svg_b = '''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000" viewBox="0 0 1000 1000">
 <defs>
  <radialGradient id="bg" cx="50%" cy="42%" r="78%">
   <stop offset="0%" stop-color="#123E5E"/>
   <stop offset="58%" stop-color="#0A2438"/>
   <stop offset="100%" stop-color="#04111C"/>
  </radialGradient>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
   <stop offset="0%" stop-color="#AFFFD8" stop-opacity="0.95"/>
   <stop offset="32%" stop-color="#2BE08C" stop-opacity="0.6"/>
   <stop offset="100%" stop-color="#2BE08C" stop-opacity="0"/>
  </radialGradient>
 </defs>
 <rect width="1000" height="1000" fill="url(#bg)"/>
 <g fill="none" stroke="#2BE08C">
  <circle cx="500" cy="500" r="410" stroke-opacity="0.50" stroke-width="7"/>
  <circle cx="500" cy="500" r="300" stroke-opacity="0.22" stroke-width="4"/>
 </g>
 <circle cx="790" cy="210" r="95" fill="url(#glow)"/>
 <circle cx="790" cy="210" r="24" fill="#EAFFF3"/>
 <text x="500" y="625" text-anchor="middle" font-family="DejaVu Sans, Arial, sans-serif" font-weight="bold" font-size="340" fill="#EAFFF3">TR</text>
</svg>'''

for name, svg in [("avatar_a", svg_a), ("avatar_b", svg_b)]:
    with open(name + ".svg", "w") as f:
        f.write(svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=name + ".png",
                     output_width=1000, output_height=1000)
    print("wrote", name + ".png")
