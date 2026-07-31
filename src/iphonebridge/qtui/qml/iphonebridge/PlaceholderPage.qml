import QtQuick
import iphonebridge

// Stand-in until calls / notifications / setup are ported.
Item {
    property string label: ""
    Text {
        anchors.centerIn: parent
        text: parent.label + " — not ported yet"
        color: Theme.textDim
        font.pixelSize: 16
    }
}
