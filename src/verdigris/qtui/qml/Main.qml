import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import verdigris

// Root is a plain Item, not an ApplicationWindow: the window itself is a
// QMainWindow on the Python side, so its QMenuBar can be exported to
// Plasma's global menu (a QQuickWindow cannot be).
Item {
    id: root

    property int pageIndex: 0
    property bool sidebarVisible: true
    // Read and written by the View menu in qtui/app.py; the conversation's
    // own context menus write the same property from the other side.
    property alias showTimes: conversations.showTimes

    // ---- global menu bridge ---------------------------------------------
    // qtui/app.py owns the menu bar (a QMenuBar exported over dbusmenu) and
    // drives the scene through this one entry point, invoked with
    // QMetaObject.invokeMethod. The `string` annotation matters: without it
    // the function's meta-signature takes a QVariant and the invoke misses.
    function menuAction(name: string) {
        switch (name) {
        case "find":            conversations.focusSearch(); break
        case "composer":        conversations.focusComposer(); break
        case "clearThread":     conversations.clearCurrentConversation(); break
        case "togglePin":
            if (threadStore.currentKey !== "")
                threadStore.togglePin(threadStore.currentKey)
            break
        case "markRead":        threadStore.markCurrentRead(); break
        case "biggerText":      Theme.fontScale = Math.min(2.0, Theme.fontScale + 0.1); break
        case "smallerText":     Theme.fontScale = Math.max(0.7, Theme.fontScale - 0.1); break
        case "actualSizeText":  Theme.fontScale = 1.0; break
        // Undo/cut/copy/paste act on whatever text field has focus. QML's
        // editors all expose these as methods, so dispatch by name rather
        // than tracking the focused item ourselves.
        case "undo": case "redo": case "cut": case "copy":
        case "paste": case "selectAll":
            var item = root.Window.activeFocusItem
            if (item && typeof item[name] === "function")
                item[name]()
            break
        default:
            console.warn("unknown menu action:", name)
        }
    }

    // Resize grips live at the *end* of the file so they sit on top of the
    // content in the sibling stacking order — see below the main Rectangle.

    Rectangle {
        anchors.fill: parent
        color: Theme.pageBg
        radius: 10

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // No window-wide header: the only nav row lives at the top of
            // the conversation sidebar (see ConversationsPage). Other pages
            // get a bare chrome strip so the window is still movable and
            // closable from them. Fades with the page switch so chrome does
            // not hard-cut while the page stack crossfades.
            WindowChrome {
                Layout.fillWidth: true
                Layout.preferredHeight: root.pageIndex !== 0 ? implicitHeight : 0
                opacity: root.pageIndex !== 0 ? 1 : 0
                visible: Layout.preferredHeight > 0.5 || opacity > 0.01
                clip: true
                pageIndex: root.pageIndex
                onPageRequested: (i) => root.pageIndex = i
                Behavior on Layout.preferredHeight {
                    NumberAnimation {
                        duration: Theme.animBase
                        easing.type: Easing.OutCubic
                    }
                }
                Behavior on opacity {
                    NumberAnimation { duration: Theme.animBase }
                }
            }

            // ---- pages -------------------------------------------------
            // StackLayout only shows one child and hard-cuts. An overlay
            // host keeps every page mounted and crossfades opacity so
            // Messages ↔ placeholders feel continuous.
            Item {
                id: pageHost
                Layout.fillWidth: true
                Layout.fillHeight: true

                ConversationsPage {
                    id: conversations
                    anchors.fill: parent
                    sidebarVisible: root.sidebarVisible
                    pageIndex: root.pageIndex
                    onPageRequested: (i) => root.pageIndex = i
                    opacity: root.pageIndex === 0 ? 1 : 0
                    visible: opacity > 0.01
                    enabled: root.pageIndex === 0
                    z: root.pageIndex === 0 ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                PlaceholderPage {
                    anchors.fill: parent
                    label: "Notifications"
                    opacity: root.pageIndex === 1 ? 1 : 0
                    visible: opacity > 0.01
                    enabled: root.pageIndex === 1
                    z: root.pageIndex === 1 ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                PlaceholderPage {
                    anchors.fill: parent
                    label: "Calls"
                    opacity: root.pageIndex === 2 ? 1 : 0
                    visible: opacity > 0.01
                    enabled: root.pageIndex === 2
                    z: root.pageIndex === 2 ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                PlaceholderPage {
                    anchors.fill: parent
                    label: "Setup"
                    opacity: root.pageIndex === 3 ? 1 : 0
                    visible: opacity > 0.01
                    enabled: root.pageIndex === 3
                    z: root.pageIndex === 3 ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                }
            }
        }
    }

    // ---- resize grips ---------------------------------------------------
    // Frameless windows get no border from KWin, so we provide the grab
    // zones. Declared last (and with a high z) so they stay above the
    // content regardless of what the pages do with their own stacking.
    //
    // Qt.Edges: left 1, right 2, top 4, bottom 8.
    QtObject {
        id: grip
        readonly property int thickness: 14   // 6px was near-impossible to hit
        readonly property int corner: 22
    }

    Repeater {
        model: [
            { e: 1,     ax: "l", ay: "s", cur: Qt.SizeHorCursor },
            { e: 2,     ax: "r", ay: "s", cur: Qt.SizeHorCursor },
            { e: 4,     ax: "s", ay: "t", cur: Qt.SizeVerCursor },
            { e: 8,     ax: "s", ay: "b", cur: Qt.SizeVerCursor },
            { e: 1 | 4, ax: "l", ay: "t", cur: Qt.SizeFDiagCursor },
            { e: 2 | 8, ax: "r", ay: "b", cur: Qt.SizeFDiagCursor },
            { e: 2 | 4, ax: "r", ay: "t", cur: Qt.SizeBDiagCursor },
            { e: 1 | 8, ax: "l", ay: "b", cur: Qt.SizeBDiagCursor }
        ]
        delegate: MouseArea {
            required property var modelData
            readonly property bool isCorner: modelData.ax !== "s" && modelData.ay !== "s"
            readonly property int t: isCorner ? grip.corner : grip.thickness

            z: 9999
            hoverEnabled: true
            cursorShape: modelData.cur
            // Don't let a page's own MouseArea steal the drag mid-gesture.
            preventStealing: true

            x: modelData.ax === "l" ? 0
             : modelData.ax === "r" ? root.width - t
             : grip.corner
            y: modelData.ay === "t" ? 0
             : modelData.ay === "b" ? root.height - t
             : grip.corner
            width:  modelData.ax === "s" ? Math.max(0, root.width  - 2 * grip.corner) : t
            height: modelData.ay === "s" ? Math.max(0, root.height - 2 * grip.corner) : t

            onPressed: winctl.startResize(modelData.e)
        }
    }
}
