# Asset provenance

The product visualizations are crops/derivatives of the supplied CallBox concept infographic generated earlier in this conversation. They are concept illustrations, not engineering drawings or photos of a manufactured device. The original sheet is retained as web/site/assets/original-concept.webp.

The original marketing website and its local assets are preserved under web/site/. The console reuses callbox-device.webp. Its small brand symbol and UI icons are local SVG/CSS elements, with no remote image dependencies. CSS uses system fonts; no font files are distributed.

qa/ screenshots come from rendering this release's application code against a real local API using the explicitly labelled restricted-browser relay. Their synthetic records are not customer data.

CallBox-Website.html is the earlier standalone product demonstration. It is not the server-backed console and does not connect to the new API when opened as a file.
