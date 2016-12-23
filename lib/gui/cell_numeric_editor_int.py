from seamless.qt.QtWidgets import QSpinBox, QWidget, QVBoxLayout
from seamless.qt.QtCore import Qt

w = QWidget()
w.setWindowFlags(Qt.WindowStaysOnTopHint)
w.setAttribute(Qt.WA_ShowWithoutActivating)
vbox = QVBoxLayout()
vbox.addStretch(1)
w.setLayout(vbox)
w.resize(300,100)
w.show()
b = QSpinBox()
b.setMaximum(1000000)
vbox.addWidget(b)
b.valueChanged.connect(output.set)
_cache["b"] = b
_cache["w"] = w
