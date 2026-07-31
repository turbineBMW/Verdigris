import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import QtQuick.Layouts
import QtQuick.Shapes
// Qt5Compat.GraphicalEffects exports a LinearGradient of its own with a
// different API, and being imported later it wins the unqualified name.
import QtQuick.Shapes as Shapes
import QtQuick.Window
import Qt5Compat.GraphicalEffects
import iphonebridge

Item {
    id: page
    property bool sidebarVisible: true
    property int pageIndex: 0
    signal pageRequested(int index)

    // Clicking away from something drops it. Without this a message stayed
    // highlighted and the composer kept its caret no matter where you clicked
    // next, so the window always looked like it was mid-edit.
    //
    // Driven off `Window.activeFocusItem` rather than off what was clicked,
    // because "outside" is a property of the thing holding focus, not of the
    // thing under the cursor — every background, header and gap would
    // otherwise need its own handler. A press inside the focus item's own
    // rectangle is left alone; anything else clears.
    function dropStrayFocus(scenePos) {
        var item = Window.activeFocusItem
        if (!item)
            return
        var p = item.mapFromItem(null, scenePos)
        if (p.x >= 0 && p.y >= 0 && p.x <= item.width && p.y <= item.height)
            return
        // persistentSelection keeps a message highlighted after it loses focus
        // — that is what lets the context menu copy it, and it also means
        // dropping focus alone would leave the highlight behind.
        if (item.deselect)
            item.deselect()
        page.forceActiveFocus()
    }

    // Deliberately the topmost child and a MouseArea rather than a TapHandler
    // on the root: a handler on an ancestor never saw the press at all, the
    // list and the message text having taken it first. This sits above
    // everything, looks at the press, and then refuses it — `accepted = false`
    // sends the whole sequence on to whatever is underneath, so scrolling,
    // selection and every button still behave exactly as before.
    MouseArea {
        anchors.fill: parent
        z: 1000
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onPressed: (mouse) => {
            var scenePos = mapToItem(null, mouse.x, mouse.y)
            page.dropStrayFocus(scenePos)
            msgArea.dismissMenu(scenePos)
            // Search / reply-quote rim is not keyboard focus — drop it on
            // any click so it cannot stick after the user has moved on.
            // Runs on press (before a quote's TapHandler), so a new jump
            // still lights its target on the subsequent tap.
            threadStore.clearHighlight()
            mouse.accepted = false
        }
    }

    // ---- entry points for the global menu bar ---------------------------
    // Show Times is reachable from three places — both context menus and the
    // View menu — so the flag lives in one spot and everything aliases to it
    // rather than keeping its own copy to fall out of sync.
    property alias showTimes: msgArea.showTimes

    // The menu lives on the QMainWindow (see qtui/app.py) and reaches the
    // scene through Main.qml's menuAction(), which forwards to these.

    function focusSearch() {
        searchField.forceActiveFocus()
        searchField.selectAll()
    }

    function focusComposer() {
        if (msgArea.hasThread)
            composeField.forceActiveFocus()
    }

    // Same confirmation the sidebar's context menu uses — clearing is
    // local-only but still looks like deletion, so it always asks.
    function clearCurrentConversation() {
        if (threadStore.currentKey === "")
            return
        confirmDelete.threadKey = threadStore.currentKey
        confirmDelete.threadName = threadStore.peerName
        confirmDelete.open()
    }

    // Deleting a thread is destructive from the user's point of view even
    // though it only clears it locally, so confirm and say so explicitly.
    Dialog {
        id: confirmDelete
        property string threadKey: ""
        property string threadName: ""

        anchors.centerIn: parent
        modal: true
        title: "Delete conversation?"
        standardButtons: Dialog.Cancel | Dialog.Discard
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        enter: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "opacity"; from: 0; to: 1
                    duration: Theme.animFast
                }
                NumberAnimation {
                    property: "scale"; from: 0.96; to: 1
                    duration: Theme.animFast
                    easing.type: Easing.OutCubic
                }
            }
        }
        exit: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "opacity"; to: 0
                    duration: Theme.animFast
                }
                NumberAnimation {
                    property: "scale"; to: 0.96
                    duration: Theme.animFast
                }
            }
        }

        onDiscarded: {
            threadStore.deleteThread(threadKey)
            close()
        }

        contentItem: ColumnLayout {
            spacing: 6
            Text {
                text: "Remove “" + confirmDelete.threadName + "” from this list?"
                color: Theme.text
                font.pixelSize: 13
                wrapMode: Text.Wrap
                Layout.maximumWidth: 330
            }
            Text {
                text: "This only clears it here — nothing is deleted on your "
                      + "iPhone, and a new message will bring it back."
                color: Theme.textDim
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Layout.maximumWidth: 330
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // ---- left: pinned grid + thread list ---------------------------
        // Floating card rather than a full-height panel: inset on all
        // sides, rounded, with a soft shadow so it reads as sitting above
        // the conversation.
        Item {
            id: sidebarShell
            Layout.preferredWidth: page.sidebarVisible ? 292 : 0
            Layout.fillHeight: true
            Layout.margins: 10
            Layout.rightMargin: 4
            // Width already eases; opacity rides with it so chrome does not
            // look crushed into a thin strip mid-collapse (and so reopen is
            // a fade-in rather than content popping at full opacity in a
            // zero-width clip).
            opacity: page.sidebarVisible ? 1 : 0
            visible: Layout.preferredWidth > 0.5 || opacity > 0.01
            // Shadow lives on a *static* plate, not on the scrolling list.
            // Layering the whole sidebar (old approach) re-rasterized every
            // conversation row into an FBO on each wheel notch.
            Behavior on Layout.preferredWidth {
                NumberAnimation { duration: Theme.animBase; easing.type: Easing.OutCubic }
            }
            Behavior on opacity {
                NumberAnimation { duration: Theme.animBase }
            }

            Rectangle {
                id: sidebarShadow
                anchors.fill: parent
                radius: 14
                color: Theme.sidebarBg
                layer.enabled: true
                layer.effect: MultiEffect {
                    shadowEnabled: true
                    shadowColor: "black"
                    shadowOpacity: 0.35
                    shadowBlur: 0.6
                    shadowVerticalOffset: 2
                }
            }

            Rectangle {
                id: sidebar
                anchors.fill: parent
                radius: 14
                color: Theme.sidebarBg
                clip: true

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0

                // The window's nav row lives here, at the top of the
                // sidebar — traffic lights and the hamburger, nothing else.
                WindowChrome {
                    Layout.fillWidth: true
                    pageIndex: page.pageIndex
                    onPageRequested: (i) => page.pageRequested(i)
                }

                // Search — type-as-you-go FTS into ThreadStore.
                Rectangle {
                    Layout.fillWidth: true
                    Layout.leftMargin: 10
                    Layout.rightMargin: 10
                    Layout.topMargin: 2
                    Layout.bottomMargin: 8
                    Layout.preferredHeight: 30
                    radius: height / 2
                    color: Qt.rgba(1, 1, 1, 0.07)
                    border.width: searchField.activeFocus ? 1.5 : 1
                    border.color: searchField.activeFocus
                                  ? Theme.accent : Theme.separator
                    Behavior on border.color {
                        ColorAnimation { duration: Theme.animFast }
                    }

                    // Anchored rather than a Row: TextField carries its
                    // own left padding, which pushed the placeholder away
                    // from the icon and left them looking unrelated.
                    Icon {
                        id: searchIconBox
                        name: "search"
                        size: 14
                        color: Theme.textDim
                        anchors {
                            left: parent.left
                            leftMargin: 11
                            verticalCenter: parent.verticalCenter
                        }
                    }

                    TextField {
                        id: searchField
                        anchors {
                            left: searchIconBox.right
                            leftMargin: 7
                            // Use `shown`, not `visible`: visible lags the
                            // fade, and resizing the field to the opacity
                            // curve made the caret jump while the × eased in.
                            right: clearSearch.shown ? clearSearch.left : parent.right
                            rightMargin: clearSearch.shown ? 4 : 10
                            verticalCenter: parent.verticalCenter
                        }
                        height: parent.height
                        leftPadding: 0
                        rightPadding: 0
                        topPadding: 0
                        bottomPadding: 0
                        background: null
                        color: Theme.text
                        placeholderText: "Search"
                        placeholderTextColor: Theme.textDim
                        font.pixelSize: 12
                        verticalAlignment: TextInput.AlignVCenter
                        // Live results: every keystroke pushes into
                        // ThreadStore, which runs FTS off-thread and
                        // rebinds the result list as soon as each query
                        // returns (stale slower queries are dropped).
                        onTextChanged: threadStore.setSearchQuery(text)
                        Keys.onEscapePressed: {
                            text = ""
                            page.forceActiveFocus()
                        }
                    }

                    // Clear the query — matches the "×" affordance in iOS search.
                    Text {
                        id: clearSearch
                        anchors {
                            right: parent.right
                            rightMargin: 12
                            verticalCenter: parent.verticalCenter
                        }
                        // Opacity/scale rather than a hard visible flip so the
                        // button eases in with the first character instead of
                        // popping into the trailing edge of the field.
                        readonly property bool shown: searchField.text.length > 0
                        visible: opacity > 0.01
                        opacity: shown ? 1 : 0
                        scale: shown ? 1 : 0.85
                        Behavior on opacity {
                            NumberAnimation { duration: Theme.animFast }
                        }
                        Behavior on scale {
                            NumberAnimation {
                                duration: Theme.animFast
                                easing.type: Easing.OutCubic
                            }
                        }
                        text: "×"
                        color: Theme.textDim
                        font.pixelSize: 16
                        MouseArea {
                            anchors.fill: parent
                            anchors.margins: -6
                            // Keep the hit target alive while fading out so a
                            // late click still clears rather than missing.
                            enabled: clearSearch.shown
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                searchField.text = ""
                                searchField.forceActiveFocus()
                            }
                        }
                    }
                }

                // Pinned conversations, avatars only — the iOS grid. Capped
                // at nine by the store, which wraps to three rows here.
                // Collapses while searching so results take the full list —
                // height/opacity ease rather than a hard cut so search entry
                // does not yank the thread list under the cursor.
                Item {
                    id: pinnedFlow
                    Layout.fillWidth: true
                    Layout.topMargin: wantShown ? 12 : 0
                    Layout.bottomMargin: wantShown ? 6 : 0
                    readonly property bool wantShown:
                        threadStore.pinnedCount > 0 && !threadStore.searchActive
                    implicitHeight: wantShown ? pinnedGrid.height : 0
                    opacity: wantShown ? 1 : 0
                    // Stay painted while collapsing so the ease is visible.
                    visible: implicitHeight > 0.5 || opacity > 0.01
                    clip: true
                    Behavior on implicitHeight {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                    Behavior on opacity {
                        NumberAnimation { duration: Theme.animBase }
                    }
                    Behavior on Layout.topMargin {
                        NumberAnimation { duration: Theme.animBase }
                    }
                    Behavior on Layout.bottomMargin {
                        NumberAnimation { duration: Theme.animBase }
                    }

                    // Centre the block as a whole: lay the tiles out in a
                    // grid sized to its own columns, then centre that.
                    Grid {
                        id: pinnedGrid
                        anchors.horizontalCenter: parent.horizontalCenter
                        // Keep the grid at the top while the shell collapses
                        // so tiles fade upward rather than floating mid-gap.
                        anchors.top: parent.top
                        spacing: 10

                        // Tiles grow to fill the sidebar. Up to three across
                        // like iOS, so a handful of pins get large avatars
                        // and a full nine still fit in three tidy rows.
                        readonly property int available: pinnedFlow.width - 20
                        columns: Math.max(1, Math.min(3, threadStore.pinnedCount))
                        readonly property real tile:
                            (available - spacing * (columns - 1)) / columns
                        // Tiles still divide the full width evenly — the
                        // avatar just sits smaller inside its tile, so the
                        // spacing stays uniform edge to edge.
                        readonly property int avatarSize:
                            Math.max(36, Math.min(Math.round(tile) - 18, 64))

                        // Avatar + label + breathing room. The padding is not
                        // decorative: the highlight is inset by 2, and the
                        // avatar grows 6% on hover, so a tile sized to its
                        // contents put both against the rounded corner.
                        readonly property int padTop: 12
                        readonly property int padBottom: 9
                        readonly property real cellH:
                            avatarSize + 22 + padTop + padBottom

                        // Drag state. dragIndex is the tile being carried,
                        // targetIndex the slot it would land in; every other
                        // tile renders at the slot it gets pushed into, so the
                        // grid shows the result before the drop commits.
                        property int dragIndex: -1
                        property int targetIndex: -1

                        function cellX(i) {
                            return (i % columns) * (Math.round(tile) + spacing)
                        }
                        function cellY(i) {
                            return Math.floor(i / columns) * (cellH + spacing)
                        }
                        // Inverse of the two above: which slot a point is over.
                        function slotAt(px, py) {
                            var col = Math.floor(px / (Math.round(tile) + spacing))
                            var row = Math.floor(py / (cellH + spacing))
                            col = Math.max(0, Math.min(columns - 1, col))
                            row = Math.max(0, row)
                            return Math.max(0, Math.min(pinnedRepeater.count - 1,
                                                        row * columns + col))
                        }

                    Repeater {
                        id: pinnedRepeater
                        // The user's own pin order, not the sidebar's recency
                        // order — the grid is arrangeable by drag.
                        model: threadStore.pinnedThreads

                        delegate: Item {
                            id: pinTile
                            required property int index
                            required property var modelData

                            readonly property string threadKey: modelData.threadKey
                            readonly property bool selected:
                                threadStore.currentKey === threadKey
                            readonly property bool dragging:
                                pinnedGrid.dragIndex === index

                            // Where this tile belongs on screen: its own slot
                            // normally, shifted by one while a drag passes
                            // over it.
                            readonly property int effectiveIndex: {
                                var d = pinnedGrid.dragIndex
                                var t = pinnedGrid.targetIndex
                                if (d < 0 || t < 0)
                                    return index
                                if (index === d)
                                    return t
                                if (d < t)
                                    return (index > d && index <= t) ? index - 1 : index
                                return (index >= t && index < d) ? index + 1 : index
                            }

                            width: Math.round(pinnedGrid.tile)
                            height: pinnedGrid.cellH

                            // The Grid positions this outer Item; `content`
                            // floats inside it, so dragging never fights the
                            // positioner over the cell's x/y.
                            Item {
                                id: content
                                width: parent.width
                                height: parent.height
                                z: pinTile.dragging ? 10 : 0
                                opacity: pinTile.dragging ? 0.85 : 1.0
                                // Drag lift only on the shell — hover growth
                                // lives on the avatar so the two never fight.
                                scale: pinTile.dragging ? 1.04 : 1.0

                                x: pinnedGrid.cellX(pinTile.effectiveIndex)
                                   - pinnedGrid.cellX(pinTile.index)
                                y: pinnedGrid.cellY(pinTile.effectiveIndex)
                                   - pinnedGrid.cellY(pinTile.index)

                                // Only the tiles being pushed aside animate:
                                // the dragged one must track the cursor with
                                // no lag, and its bindings are broken anyway.
                                Behavior on x {
                                    enabled: !pinTile.dragging
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }
                                Behavior on y {
                                    enabled: !pinTile.dragging
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }
                                Behavior on scale {
                                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic }
                                }
                                Behavior on opacity {
                                    NumberAnimation { duration: Theme.animFast }
                                }

                            // Behind the Column, so the avatar and label paint
                            // on top. Selection has to key off the store's open
                            // thread: the grid is a Repeater and has no
                            // currentIndex of its own like the list below does.
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: 2
                                radius: Theme.radiusRow
                                color: pinTile.selected
                                       ? Theme.selection
                                       : (pinMouse.containsMouse || pinTile.dragging
                                          ? Qt.rgba(1, 1, 1, 0.06) : "transparent")
                                Behavior on color { ColorAnimation { duration: Theme.animFast } }
                            }

                            Column {
                                anchors.top: parent.top
                                anchors.topMargin: pinnedGrid.padTop
                                anchors.horizontalCenter: parent.horizontalCenter
                                spacing: 5

                                Avatar {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    size: pinnedGrid.avatarSize
                                    source: pinTile.modelData.avatar
                                    initials: pinTile.modelData.initials
                                    // Pad around the tile was sized for this
                                    // 6% hover growth so the highlight rim
                                    // and scaled face stay inside the corner.
                                    scale: (!pinTile.dragging && pinMouse.containsMouse)
                                           ? 1.06 : 1.0
                                    Behavior on scale {
                                        NumberAnimation {
                                            duration: Theme.animFast
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                }
                                // Name, with an unread dot to the left of it.
                                //
                                // The name is centred under the avatar and
                                // the dot hangs off its left edge, rather
                                // than the two sharing a centred Row: in a
                                // Row the dot's width and spacing push the
                                // name 5px right of centre whenever there is
                                // one, so a thread going unread visibly
                                // shifted its own label. The dot keeps its
                                // place beside the name either way, which is
                                // the part a Row was there for.
                                Item {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    width: parent.width
                                    height: pinName.height
                                    readonly property bool unread:
                                        pinTile.modelData.unread > 0

                                    Text {
                                        id: pinName
                                        // Sized to the name, not to the tile.
                                        // A tile-wide box centred the text
                                        // inside *itself*, which left the dot
                                        // anchored way out at the tile's edge
                                        // instead of beside the name. `room`
                                        // caps it — with space for the dot on
                                        // both sides so centring survives it
                                        // — so a long name elides rather than
                                        // pushing the dot out of view.
                                        readonly property int room:
                                            Math.round(pinnedGrid.tile) - 4
                                            - (parent.unread ? 20 : 0)
                                        width: Math.min(implicitWidth, room)
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        horizontalAlignment: Text.AlignHCenter
                                        elide: Text.ElideRight
                                        text: pinTile.modelData.name
                                        color: pinTile.selected
                                               ? Qt.rgba(1, 1, 1, 0.9) : Theme.textDim
                                        font.pixelSize: pinnedGrid.avatarSize > 60 ? 12 : 11
                                    }

                                    Rectangle {
                                        width: 6; height: 6; radius: 3
                                        color: Theme.accent
                                        // Same fade the list-row unread dot
                                        // uses — a hard visible flip next to
                                        // the name read as a blink.
                                        opacity: parent.unread ? 1 : 0
                                        visible: opacity > 0.01
                                        Behavior on opacity {
                                            NumberAnimation { duration: Theme.animBase }
                                        }
                                        anchors.right: pinName.left
                                        anchors.rightMargin: 4
                                        anchors.verticalCenter: pinName.verticalCenter
                                    }
                                }
                            }

                            MouseArea {
                                id: pinMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                cursorShape: pinTile.dragging
                                             ? Qt.ClosedHandCursor : Qt.ArrowCursor
                                drag.target: content
                                drag.axis: Drag.XAndYAxis
                                drag.threshold: 6
                                drag.smoothed: false

                                // Set once the pointer passes the drag
                                // threshold: `clicked` still fires after a
                                // drag, and without this every reorder would
                                // also open a conversation.
                                property bool moved: false

                                function restoreBindings() {
                                    // Dragging assigned content.x/y directly,
                                    // which broke their bindings — without
                                    // this the tile stays where it was dropped
                                    // and the reorder shifts it a second time.
                                    content.x = Qt.binding(function () {
                                        return pinnedGrid.cellX(pinTile.effectiveIndex)
                                             - pinnedGrid.cellX(pinTile.index)
                                    })
                                    content.y = Qt.binding(function () {
                                        return pinnedGrid.cellY(pinTile.effectiveIndex)
                                             - pinnedGrid.cellY(pinTile.index)
                                    })
                                }

                                onPressed: (m) => {
                                    moved = false
                                    if (m.button === Qt.LeftButton) {
                                        pinnedGrid.dragIndex = pinTile.index
                                        pinnedGrid.targetIndex = pinTile.index
                                    }
                                }
                                onPositionChanged: {
                                    if (!drag.active)
                                        return
                                    moved = true
                                    // Centre of the floating tile in grid
                                    // coordinates: its own slot, plus however
                                    // far the drag has carried it.
                                    var cx = pinTile.x + content.x + pinTile.width / 2
                                    var cy = pinTile.y + content.y + pinTile.height / 2
                                    pinnedGrid.targetIndex = pinnedGrid.slotAt(cx, cy)
                                }
                                onReleased: {
                                    if (pinnedGrid.dragIndex < 0)
                                        return
                                    var from = pinnedGrid.dragIndex
                                    var to = pinnedGrid.targetIndex
                                    pinnedGrid.dragIndex = -1
                                    pinnedGrid.targetIndex = -1
                                    restoreBindings()
                                    if (moved && from !== to)
                                        threadStore.movePin(from, to)
                                }
                                onCanceled: {
                                    pinnedGrid.dragIndex = -1
                                    pinnedGrid.targetIndex = -1
                                    restoreBindings()
                                }
                                onClicked: (m) => {
                                    if (moved)
                                        return
                                    if (m.button === Qt.RightButton)
                                        threadStore.togglePin(pinTile.threadKey)
                                    else
                                        threadStore.openThread(pinTile.threadKey)
                                }
                            }
                            }
                        }
                    }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: pinnedFlow.wantShown ? 1 : 0
                    color: Theme.separator
                    opacity: pinnedFlow.wantShown ? 1 : 0
                    visible: Layout.preferredHeight > 0.5 || opacity > 0.01
                    Behavior on Layout.preferredHeight {
                        NumberAnimation { duration: Theme.animBase }
                    }
                    Behavior on opacity {
                        NumberAnimation { duration: Theme.animBase }
                    }
                }

                // Empty search state
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 24
                    horizontalAlignment: Text.AlignHCenter
                    // Fade rather than pop when FTS returns nothing.
                    readonly property bool shown:
                        threadStore.searchActive && threadList.count === 0
                    opacity: shown ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity {
                        NumberAnimation { duration: Theme.animBase }
                    }
                    text: "No results"
                    color: Theme.textDim
                    font.pixelSize: 13
                }

                SmoothListView {
                    id: threadList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.topMargin: 6
                    clip: true
                    spacing: 2
                    reuseItems: true
                    cacheBuffer: Math.max(400, height * 2)
                    // Search replaces the conversation list with per-message
                    // hits (one thread may appear many times).
                    model: threadStore.searchActive
                           ? threadStore.searchModel
                           : threadStore.threadModel
                    // The store restores the newest thread on launch.
                    currentIndex: 0
                    // No visible scrollbar in the conversation list.
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOff }

                    // Real model inserts/deletes only — a model reset (search
                    // enter/exit, page load) raises no add transition, so the
                    // full list does not cascade-animate on every query.
                    add: Transition {
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; from: 0; to: 1
                                duration: Theme.animBase
                                easing.type: Easing.OutCubic
                            }
                            NumberAnimation {
                                property: "x"; from: -8; to: 0
                                duration: Theme.animBase
                                easing.type: Easing.OutCubic
                            }
                        }
                    }
                    remove: Transition {
                        NumberAnimation {
                            property: "opacity"; to: 0
                            duration: Theme.animFast
                        }
                    }
                    displaced: Transition {
                        NumberAnimation {
                            properties: "y"
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }

                    // A model reset (new page, thread added/removed) still
                    // drops contentY to 0. Put it back so the list doesn't
                    // jump to the top under the user — except when entering
                    // or leaving search, where starting at the top is right.
                    property real savedY: 0
                    property bool searching: threadStore.searchActive
                    onSearchingChanged: contentY = 0
                    Connections {
                        target: threadStore.threadModel
                        function onModelAboutToBeReset() {
                            if (!threadStore.searchActive)
                                threadList.savedY = threadList.contentY
                        }
                        function onModelReset() {
                            if (threadStore.searchActive)
                                return
                            Qt.callLater(function () {
                                threadList.contentY = Math.max(
                                    0, Math.min(threadList.savedY,
                                                Math.max(0, threadList.contentHeight
                                                            - threadList.height)))
                            })
                        }
                    }

                    // Infinite scroll: pull in the next page as the end
                    // of the list comes into view. Guarded on the row count
                    // so it fires once per page — contentYChanged runs every
                    // frame while scrolling, and asking for another page on
                    // each one reset the model repeatedly mid-scroll.
                    property int _lastPageRequest: 0
                    onContentYChanged: {
                        if (threadStore.searchActive)
                            return
                        if (contentHeight > 0
                                && contentY + height > contentHeight - 400
                                && count !== _lastPageRequest) {
                            _lastPageRequest = count
                            threadStore.loadMoreThreads()
                        }
                    }

                    delegate: Item {
                        id: threadDelegate
                        required property int index
                        required property string threadKey
                        required property string name
                        required property string preview
                        required property string stamp
                        required property int unread
                        required property string avatar
                        required property string initials
                        required property bool pinned
                        required property int eventId
                        required property string guid
                        required property string richPreview

                        // Selection follows the store, not `currentIndex`:
                        // opening a chat from the pinned grid never touches
                        // the list's index, so an index-based highlight left
                        // the previously opened row lit alongside the pin.
                        readonly property bool selected:
                            !threadStore.searchActive
                            && threadStore.currentKey === threadKey

                        // A pinned conversation moves into the grid above
                        // rather than appearing in both places — but search
                        // results always show, even if that thread is pinned.
                        // Height/opacity ease on pin so the row collapses
                        // instead of vanishing in one frame (ListView remove
                        // only fires when the model drops the row entirely).
                        width: threadList.width
                        readonly property bool shown:
                            threadStore.searchActive || !pinned
                        height: shown ? 58 : 0
                        opacity: shown ? 1 : 0
                        visible: height > 0.5 || opacity > 0.01
                        clip: true
                        Behavior on height {
                            NumberAnimation {
                                duration: Theme.animFast
                                easing.type: Easing.OutCubic
                            }
                        }
                        Behavior on opacity {
                            NumberAnimation { duration: Theme.animFast }
                        }

                        Rectangle {
                            anchors {
                                fill: parent
                                leftMargin: 6; rightMargin: 6
                                topMargin: 1; bottomMargin: 1
                            }
                            radius: Theme.radiusRow
                            color: threadDelegate.selected
                                   ? Theme.selection
                                   : (rowMouse.containsMouse
                                      ? Qt.rgba(1, 1, 1, 0.06) : "transparent")

                            Behavior on color { ColorAnimation { duration: Theme.animFast } }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 4
                                anchors.rightMargin: 10
                                spacing: 8

                                // Unread dot in the gutter, left of the avatar.
                                Rectangle {
                                    Layout.preferredWidth: 8
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: Theme.accent
                                    opacity: (!threadStore.searchActive && unread > 0) ? 1 : 0
                                    Behavior on opacity { NumberAnimation { duration: Theme.animBase } }
                                }

                                Avatar {
                                    size: 40
                                    source: threadDelegate.avatar
                                    initials: threadDelegate.initials
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 6
                                        Text {
                                            Layout.fillWidth: true
                                            text: threadDelegate.name
                                            elide: Text.ElideRight
                                            color: Theme.text
                                            font.pixelSize: 13
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            text: threadDelegate.stamp
                                            color: threadDelegate.selected
                                                   ? Qt.rgba(1, 1, 1, 0.8) : Theme.textDim
                                            font.pixelSize: 11
                                        }
                                    }
                                    // Search rows use RichText so FTS hits are
                                    // bold; conversation rows stay plain.
                                    Text {
                                        Layout.fillWidth: true
                                        text: threadStore.searchActive
                                              && threadDelegate.richPreview.length > 0
                                              ? threadDelegate.richPreview
                                              : threadDelegate.preview
                                        textFormat: threadStore.searchActive
                                                    ? Text.RichText
                                                    : Text.PlainText
                                        elide: Text.ElideRight
                                        maximumLineCount: 1
                                        color: threadDelegate.selected
                                               ? Qt.rgba(1, 1, 1, 0.8) : Theme.textDim
                                        font.pixelSize: 12
                                    }
                                }
                            }

                            MouseArea {
                                id: rowMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                onClicked: (m) => {
                                    if (threadStore.searchActive) {
                                        if (m.button === Qt.LeftButton) {
                                            threadStore.openSearchResult(
                                                threadDelegate.threadKey,
                                                threadDelegate.eventId,
                                                threadDelegate.guid)
                                        }
                                        return
                                    }
                                    if (m.button === Qt.RightButton) {
                                        rowMenu.popup()
                                    } else {
                                        threadList.currentIndex = threadDelegate.index
                                        threadStore.openThread(threadDelegate.threadKey)
                                    }
                                }

                                Menu {
                                    id: rowMenu
                                    enter: Transition {
                                        ParallelAnimation {
                                            NumberAnimation {
                                                property: "opacity"; from: 0; to: 1
                                                duration: Theme.animFast
                                            }
                                            NumberAnimation {
                                                property: "scale"
                                                from: Theme.popScale; to: 1
                                                duration: Theme.animFast
                                                easing.type: Easing.OutCubic
                                            }
                                        }
                                    }
                                    exit: Transition {
                                        NumberAnimation {
                                            property: "opacity"; to: 0
                                            duration: Theme.animMicro
                                        }
                                    }
                                    MenuItem {
                                        text: threadDelegate.pinned ? "Unpin" : "Pin"
                                        onTriggered: threadStore.togglePin(
                                            threadDelegate.threadKey)
                                    }
                                    MenuSeparator {}
                                    MenuItem {
                                        text: "Delete Conversation"
                                        onTriggered: {
                                            confirmDelete.threadKey =
                                                threadDelegate.threadKey
                                            confirmDelete.threadName =
                                                threadDelegate.name
                                            confirmDelete.open()
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            } // sidebar
        } // sidebarShell

        // No divider — the sidebar floats, so its own edge separates it.

        // ---- right: peer header + bubbles + compose --------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Messages, with the peer header floating over them: no hard
            // rule, just a fade so bubbles dissolve as they scroll under it.
            Item {
                id: msgArea
                Layout.fillWidth: true
                Layout.fillHeight: true

                readonly property bool hasThread: threadStore.peerName !== ""
                readonly property int headerHeight: 70

                // Show Times, as in Messages.app: the whole right-hand side of
                // the conversation slides left to open a gutter, and every
                // message — outgoing and incoming alike — gets the time it was
                // sent in it.
                //
                // Held here rather than per-delegate so it survives delegates
                // being destroyed and rebuilt as you scroll, and so the right
                // gutter is one number the whole thread agrees on.
                property bool showTimes: false
                // Wide enough for the longest stamp ("12:24 PM" is 48px at
                // this size) plus the margin outside it and a clear gap
                // between the stamp and the bubble it belongs to.
                readonly property int timeGutter: 74

                // The bubble menu that is currently open, if any. One at a
                // time, and the page has to be able to reach it: the menus
                // are non-modal, so closing one when you press elsewhere is
                // this page's job rather than the overlay's.
                property var openMenu: null

                // Close the open bubble menu unless the press landed inside
                // it. Called for every press on the page, before the item
                // under the cursor gets a look — so a right-click on another
                // bubble dismisses this menu and opens that one, in the same
                // gesture rather than needing two.
                function dismissMenu(scenePos) {
                    if (!openMenu || !openMenu.visible)
                        return
                    var bg = openMenu.background
                    if (bg) {
                        var p = bg.mapFromItem(null, scenePos)
                        if (p.x >= 0 && p.y >= 0
                                && p.x <= bg.width && p.y <= bg.height)
                            return
                    }
                    openMenu.close()
                }

                // Send an arbitrary emoji as a tapback and put the menu away.
                // Shared because the picker fires it from a tap and from
                // Enter, and duplicating the pair is how one of them ends up
                // leaving the menu open.
                function sendReaction(guid, emojiChar, menu) {
                    threadStore.react(guid, emojiChar)
                    menu.close()
                }

                // Empty state — fades with the conversation chrome so
                // opening a thread is one surface change, not a hard cut.
                Text {
                    anchors.centerIn: parent
                    opacity: msgArea.hasThread ? 0 : 1
                    visible: opacity > 0.01
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.animFast
                            easing.type: Easing.OutCubic
                        }
                    }
                    text: "Select a conversation"
                    color: Theme.textDim
                    font.pixelSize: 18
                }

            // Right-click anywhere in the conversation, not only on a
            // bubble. Declared before the list so it sits beneath it: the
            // Flickable takes the left button for dragging and lets the right
            // one through, and a bubble that wants its own menu has a
            // MouseArea of its own further up the stack.
            MouseArea {
                anchors.fill: parent
                visible: msgArea.hasThread
                acceptedButtons: Qt.RightButton
                onClicked: (mouse) => {
                    chatMenu.x = mouse.x
                    chatMenu.y = mouse.y
                    chatMenu.open()
                }

                Menu {
                    id: chatMenu
                    enter: Transition {
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; from: 0; to: 1
                                duration: Theme.animFast
                            }
                            NumberAnimation {
                                property: "scale"
                                from: Theme.popScale; to: 1
                                duration: Theme.animFast
                                easing.type: Easing.OutCubic
                            }
                        }
                    }
                    exit: Transition {
                        NumberAnimation {
                            property: "opacity"; to: 0
                            duration: Theme.animMicro
                        }
                    }
                    MenuItem {
                        text: msgArea.showTimes ? "Hide Times" : "Show Times"
                        onTriggered: msgArea.showTimes = !msgArea.showTimes
                    }
                }
            }

            SmoothListView {
                id: msgList
                anchors.fill: parent
                visible: msgArea.hasThread
                // Leave room so the first bubble starts below the header
                // rather than under it.
                topMargin: msgArea.headerHeight + 30
                // Height of the dissolve above the composer. The mask reads it.
                //
                // Kept under the caption line's 14px so the fade cannot reach
                // the "Delivered"/"Read" text, which sits in that band right
                // above the resting edge and was being dimmed out by a deeper
                // fade. The two are related: anything taller than the caption
                // either swallows it or has to be pushed clear by a margin
                // that reads as a gap above the composer.
                // A new bubble rises into place instead of appearing fully
                // formed, and anything above it glides rather than jumping.
                //
                // Only real insertions animate. Loading a thread, importing a
                // backup and every rebuild go through `reload()`, which resets
                // the model — and a reset raises no add transition, so opening
                // a conversation does not try to animate 4000 bubbles.
                // Fades in while rising the last few pixels into place. The
                // rise is `riseOffset`, a transform on the delegate rather
                // than its `y` — see the comment there for why animating the
                // layout position instead is what read as jitter.
                add: Transition {
                    SequentialAnimation {
                        // Marks the bubble as still arriving. A receipt can
                        // land inside this window — a message that is
                        // delivered almost as fast as it is sent — and then
                        // the caption's own fade-up would run at the same
                        // time as the bubble's, two overlapping animations on
                        // the same few pixels. While this is set, the caption
                        // skips its animation and simply arrives with the
                        // bubble it belongs to, as one movement.
                        PropertyAction { property: "freshlyAdded"; value: true }
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; from: 0; to: 1
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                            NumberAnimation {
                                property: "riseOffset"; from: 10; to: 0
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                        }
                        PropertyAction { property: "freshlyAdded"; value: false }
                    }
                }
                // Rows pushed along by the insertion. Without this they snap
                // to their new positions while the new bubble eases into its
                // own, which is the jitter the animation exists to remove.
                displaced: Transition {
                    NumberAnimation {
                        properties: "y"
                        duration: Theme.animBase
                        easing.type: Easing.OutCubic
                    }
                }

                readonly property int bottomFade: 6
                // Deliberately smaller than the fade rather than equal to it.
                // Matching them kept the newest bubble entirely clear of the
                // dissolve, but that is what left the thread resting visibly
                // short of the bottom. At 3 the fade laps the last few pixels
                // of the bubble — which is its rounded padding, not its text —
                // and the conversation sits where it should.
                bottomMargin: 3
                clip: true
                spacing: 2
                model: threadStore.messageModel

                // Message bubbles are all different heights, so ListView can
                // only *estimate* contentHeight from the delegates it has
                // built. Every delegate created while scrolling corrects that
                // estimate, which shifts contentY under the pointer — the
                // jitter. Building a screenful either side means heights are
                // already known by the time they matter.
                cacheBuffer: Math.max(1600, height * 3)
                // Deliberately no reuseItems. Bubbles pin with
                // `anchors.left/right: outgoing ? … : undefined`, and on
                // recycle Qt does not clear the previous side's anchor when
                // the binding becomes undefined — so a reused outgoing row
                // kept its left edge and stretched across the whole thread
                // (and incoming rows stretched off-screen to the right).

                // Scroll-up paging: when the user nears the top, pull older
                // history from SQLite. Guarded on count so we only ask once
                // per loaded page. (Handler merged with atBottom below —
                // QML forbids setting onContentYChanged twice on one item.)
                property int _olderRequestAt: -1

                // Search / reply-quote jump → park on a specific bubble.
                //
                // Two fights make the rubber-band:
                // 1. Open-thread end-aim (landAtEnd, pinBottom, contentHeight
                //    follow) snaps to maxContentY — blocked by holdScroll.
                // 2. ListView clamps contentY when height *estimates* shrink
                //    (maxContentY drops under contentY) — looks like a yank
                //    to the bottom. Fix: keep re-centering on jumpTargetEventId
                //    for the whole hold window, not only while jumpSettle runs.
                property int pendingJumpEventId: 0
                // Sticky target kept after jumpSettle ends so late estimate
                // thrash can still re-aim (pendingJumpEventId is cleared).
                property int jumpTargetEventId: 0
                property int jumpTicks: 0
                property bool landingFromSearch: false
                // Master lock for jump-to-message. Broader than
                // landingFromSearch so reply-quote jumps share it.
                property bool holdScroll: false

                function beginHoldScroll() {
                    holdScroll = true
                    landingFromSearch = true
                    pinBottom = false
                    settleToEnd.running = false
                    animateScroll = false
                    followRelease.stop()
                    pinRelease.stop()
                    justOpened = false
                    // Don't page in older history mid-jump (that rebuilds and
                    // re-triggers the end-aim race).
                    _olderRequestAt = count
                }

                function scrollToEvent(eventId) {
                    beginHoldScroll()
                    pendingJumpEventId = eventId
                    jumpTargetEventId = eventId
                    jumpTicks = 0
                    searchJumpRelease.stop()
                    if (eventId <= 0) {
                        finishSearchJump()
                        return
                    }
                    // Re-aim while heights settle. ListView only estimates
                    // until delegates exist; a single positionViewAtIndex is
                    // usually short or past the hit on a long thread.
                    jumpSettle.restart()
                    aimAtPendingJump()
                }

                function aimAtPendingJump() {
                    var eid = pendingJumpEventId > 0
                               ? pendingJumpEventId
                               : jumpTargetEventId
                    if (eid <= 0)
                        return false
                    var idx = threadStore.indexOfEvent(eid)
                    if (idx < 0)
                        return false
                    // Center on the hit. Do not touch pinBottom / settle.
                    positionViewAtIndex(idx, ListView.Center)
                    // Keep SmoothListView's wheel integrator from chasing a
                    // stale end target after we moved contentY.
                    stopScroll()
                    landed = true
                    return true
                }

                function finishSearchJump() {
                    jumpSettle.stop()
                    pendingJumpEventId = 0
                    jumpTicks = 0
                    landed = true
                    // Keep holdScroll + jumpTargetEventId well past layout
                    // thrash so clamp-to-maxContentY cannot win late.
                    searchJumpRelease.restart()
                }

                function releaseHoldScroll() {
                    landingFromSearch = false
                    holdScroll = false
                    pendingJumpEventId = 0
                    jumpTargetEventId = 0
                    jumpTicks = 0
                    jumpSettle.stop()
                }

                Timer {
                    id: searchJumpRelease
                    // Long enough for a heavy thread's delegate heights to
                    // stop thrashing; end-aim stays no-op until then.
                    interval: 3000
                    onTriggered: msgList.releaseHoldScroll()
                }

                Timer {
                    id: jumpSettle
                    interval: 16
                    repeat: true
                    onTriggered: {
                        msgList.jumpTicks += 1
                        var ok = msgList.aimAtPendingJump()
                        // ~1.2s of aggressive re-centering, then holdScroll
                        // alone re-aims on contentHeight changes.
                        if (msgList.jumpTicks >= 75
                                || (msgList.jumpTicks >= 10 && !ok
                                    && threadStore.indexOfEvent(
                                        msgList.jumpTargetEventId
                                        || msgList.pendingJumpEventId) < 0)) {
                            msgList.finishSearchJump()
                        }
                    }
                }

                Connections {
                    target: threadStore
                    function onJumpToMessage(eventId) {
                        msgList.scrollToEvent(eventId)
                    }
                }

                // `maxContentY` and the mouse-wheel handling both come from
                // SmoothListView. This used to redeclare both here, which is
                // what made the view jump to the top of the conversation:
                //
                // The local `maxContentY` shadowed the base one but computed
                // a different value — it ignored topMargin and bottomMargin,
                // so the two disagreed about where the bottom is by the
                // height of the header. Worse, only the checks written here
                // saw the shadow; scrollBy() and smoothScrollToEnd() inside
                // SmoothListView kept using the base. And when a contentHeight
                // estimate momentarily came in under `height` — routine with
                // variable-height bubbles, and most likely right after
                // appending a message — the local formula collapsed to
                // `originY`, so clamping against it slammed contentY to the
                // very top.
                //
                // The second WheelHandler was the other half: both it and
                // SmoothListView's accepted the same notch, one writing
                // contentY directly while the other drove the integrator
                // toward a target, each cancelling the other mid-scroll.
                //
                // Rubber-banding at the ends compounds the estimate shifting,
                // so the overshoot stays off here — but via SmoothListView's
                // own flag, so its wheel path honours it too rather than
                // springing toward a target StopAtBounds won't let it reach.
                boundsBehavior: Flickable.StopAtBounds
                bounceOnWheel: false
                // See `smoothWheel` in SmoothListView: the eased chase cannot
                // track this list, because realizing message delegates keeps
                // moving `originY` out from under its target.
                smoothWheel: false

                // Typing indicator: an incoming-side bubble with three
                // pulsing dots, sitting where their next message will land.
                // A footer rather than a model row so it can't be confused
                // for a message or scrolled past independently.
                //
                // Groups also get the typists' avatars stacked to the left
                // of the dots (iMessage-style overlap); 1:1 stays dots-only.
                footer: Item {
                    id: typingFooter
                    width: msgList.width
                    height: threadStore.peerTyping ? 34 : 0
                    visible: height > 0
                    Behavior on height {
                        NumberAnimation { duration: Theme.animBase; easing.type: Easing.OutCubic }
                    }

                    // ~60% step so each next avatar sits on the previous.
                    readonly property int avatarSize: 22
                    readonly property int avatarStep: Math.round(avatarSize * 0.6)
                    readonly property var typingPeople: threadStore.typingAvatars
                    readonly property int avatarCount: typingPeople ? typingPeople.length : 0
                    readonly property int avatarsWidth: avatarCount > 0
                        ? avatarSize + (avatarCount - 1) * avatarStep
                        : 0

                    Row {
                        id: typingRow
                        anchors.left: parent.left
                        anchors.leftMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 6

                        // Overlapping circular avatars for group typists.
                        Item {
                            id: typingAvatarsBox
                            visible: typingFooter.avatarCount > 0
                            width: typingFooter.avatarsWidth
                            height: typingFooter.avatarSize
                            anchors.verticalCenter: parent.verticalCenter
                            opacity: typingFooter.avatarCount > 0 ? 1 : 0
                            Behavior on opacity {
                                NumberAnimation { duration: Theme.animBase }
                            }
                            Behavior on width {
                                NumberAnimation {
                                    duration: Theme.animBase
                                    easing.type: Easing.OutCubic
                                }
                            }

                            Repeater {
                                model: typingFooter.typingPeople
                                Item {
                                    id: typingAvatarSlot
                                    required property var modelData
                                    required property int index
                                    width: typingFooter.avatarSize
                                    height: typingFooter.avatarSize
                                    x: index * typingFooter.avatarStep
                                    z: index

                                    // Thin ring so stacked faces separate
                                    // cleanly against the chat background.
                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: parent.width + 2
                                        height: parent.height + 2
                                        radius: width / 2
                                        color: Theme.pageBgSolid
                                    }
                                    Avatar {
                                        anchors.centerIn: parent
                                        size: typingFooter.avatarSize
                                        source: typingAvatarSlot.modelData.avatar || ""
                                        initials: typingAvatarSlot.modelData.initials || "?"
                                    }
                                }
                            }
                        }

                        Rectangle {
                            id: typingBubble
                            width: 52
                            height: 26
                            radius: 13
                            color: Theme.bubbleIn
                            anchors.verticalCenter: parent.verticalCenter
                            opacity: threadStore.peerTyping ? 1 : 0
                            Behavior on opacity {
                                NumberAnimation { duration: Theme.animBase }
                            }

                            Row {
                                anchors.centerIn: parent
                                spacing: 4
                                Repeater {
                                    model: 3
                                    Rectangle {
                                        required property int index
                                        width: 7; height: 7; radius: 3.5
                                        color: Theme.textDim

                                        // Staggered so the dots ripple rather
                                        // than blink in unison.
                                        SequentialAnimation on opacity {
                                            running: threadStore.peerTyping
                                            loops: Animation.Infinite
                                            PauseAnimation { duration: index * 160 }
                                            NumberAnimation { to: 1.0; duration: 300 }
                                            NumberAnimation { to: 0.35; duration: 300 }
                                            PauseAnimation { duration: (2 - index) * 160 }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // Overlay scrollbar with no groove or divider line — the
                // default style draws a track with a border, which read as
                // a stray vertical rule beside the messages.
                ScrollBar.vertical: ScrollBar {
                    id: msgScroll
                    policy: ScrollBar.AsNeeded
                    width: 8
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    background: null
                    contentItem: Rectangle {
                        implicitWidth: 6
                        radius: 3
                        color: Qt.rgba(1, 1, 1, msgScroll.pressed ? 0.45
                                                : (msgScroll.hovered ? 0.32 : 0.18))
                        opacity: msgScroll.active ? 1 : 0
                        Behavior on opacity {
                            NumberAnimation { duration: Theme.animBase }
                        }
                        Behavior on color { ColorAnimation { duration: Theme.animFast } }
                    }
                }

                // One gradient mask across the whole list, so content
                // dissolves per-pixel as it scrolls under the floating
                // header. OpacityMask (Qt5Compat) rather than MultiEffect:
                // MultiEffect's mask silently did nothing here.
                // No layer.samples: the layer is a full-viewport FBO
                // redrawn every frame while scrolling, and multisampling it
                // was costing more than it bought. Bubbles are drawn by
                // Shape's curve renderer, which antialiases analytically,
                // and text and images bring their own.
                //
                // Do not replace this with solid overlay rects: the page
                // background is translucent over KWin blur, so a solid
                // pageBgSolid gradient reads as a black bar, not a dissolve.
                layer.enabled: true
                layer.smooth: true
                layer.effect: OpacityMask {
                    maskSource: Item {
                        width: msgList.width
                        height: msgList.height
                        Rectangle {
                            anchors.fill: parent
                            gradient: Gradient {
                                GradientStop { position: 0.0; color: "transparent" }
                                GradientStop {
                                    position: Math.min(
                                        0.9, (msgArea.headerHeight + 58)
                                             / Math.max(1, msgList.height))
                                    color: "white"
                                }
                                // Mirror of the header fade, thinner: bubbles
                                // dissolve on their way down to the composer
                                // instead of meeting it at a hard edge.
                                //
                                // `bottomMargin` on the list is what keeps the
                                // newest message out of it — resting at the
                                // end leaves exactly the fade's height of
                                // empty space below the last bubble, so the
                                // fade only ever bites while scrolling back
                                // through history.
                                GradientStop {
                                    position: 1.0 - Math.min(
                                        0.3, msgList.bottomFade
                                             / Math.max(1, msgList.height))
                                    color: "white"
                                }
                                GradientStop { position: 1.0; color: "transparent" }
                            }
                        }
                    }
                }

                // A model reset drops contentY to 0, throwing the view to the
                // very top of the conversation. The message model resets on
                // any rebuild — and sending resets it twice over: the new row
                // changes the count, then the daemon's echo comes back and
                // `_upgrade_echo_guid` rebuilds to bind the guid. That is why
                // the jump was easiest to trigger by pressing send.
                //
                // Saved as distance from the *bottom*, not absolute contentY,
                // because a conversation is bottom-anchored: sitting at the
                // newest message has to stay there even though the content
                // just got taller, and sitting N px up in history has to stay
                // N px up.
                // Not restored when the reset is a *different conversation*
                // loading: the saved offset describes the thread being left,
                // so applying it to the new one lands at an arbitrary point
                // in its history instead of at the newest message.
                //
                // Told apart by which thread the current rows belong to, not
                // by `justOpened`: `_open` sets the key and rebuilds the model
                // *before* emitting peerChanged, so at reset time the flag is
                // still showing the previous thread's state. `renderedKey` is
                // only updated once rows for a thread are actually in place.
                property real savedBottom: -1
                property string renderedKey: ""
                Connections {
                    target: threadStore.messageModel
                    function onModelAboutToBeReset() {
                        msgList.savedBottom =
                            msgList.renderedKey === threadStore.currentKey
                            ? msgList.maxContentY - msgList.contentY
                            : -1
                    }
                    function onModelReset() {
                        var saved = msgList.savedBottom
                        msgList.savedBottom = -1
                        msgList.renderedKey = threadStore.currentKey
                        // Search jump owns the viewport; do not restore the
                        // previous bottom offset over the hit.
                        if (msgList.jumpLock) {
                            if (msgList.jumpTargetEventId > 0
                                    || msgList.pendingJumpEventId > 0)
                                Qt.callLater(function () {
                                    msgList.aimAtPendingJump()
                                })
                            return
                        }
                        if (saved < 0)
                            return
                        // A reset zeroes contentY. If the Behavior on
                        // contentY is still armed from a recent followToEnd
                        // (new message, then its attachments arrive a beat
                        // later and rebuild the list), restoring the saved
                        // offset would *animate* from 0 → bottom — the
                        // conversation flying up through history and
                        // gliding back down. Kill the easing first so the
                        // restore is a single frame.
                        msgList.animateScroll = false
                        followRelease.stop()
                        // Restoring to the bottom is `landAtEnd`'s job, not
                        // arithmetic against a figure that is about to change:
                        // `maxContentY` at reset time and after it are two
                        // different numbers, and subtracting across them is
                        // how a send could land at the very top.
                        if (saved < 8) {
                            // Instant pin, then settle as heights firm up.
                            // pinBottom absorbs the image-size thrash that
                            // often follows an attachment rebuild.
                            msgList.pinBottom = true
                            pinRelease.restart()
                            msgList.jumpToEnd()
                            msgList.landAtEnd()
                            return
                        }
                        Qt.callLater(function () {
                            if (msgList.jumpLock)
                                return
                            msgList.animateScroll = false
                            msgList.contentY = Math.max(
                                msgList.minContentY,
                                Math.min(msgList.maxContentY,
                                         msgList.maxContentY - saved))
                        })
                    }
                }

                // Landing at the end of a long thread takes several attempts.
                // `contentHeight` is only an estimate until every delegate has
                // been built, and with message bubbles it does not converge
                // quietly: measured on a real thread it went 96k → 116k → 76k
                // px while opening. So a single positionViewAtEnd() aims at a
                // figure that is already wrong, and the view stops somewhere
                // arbitrary — 2000px up the conversation, looking random.
                //
                // Re-aim every frame until it sticks. Bounded by a tick count
                // so a thread whose height never settles can't hold the view.
                // This loop only re-aims the *target* of the eased scroll; the
                // motion itself is the Behavior on contentY, which Qt advances
                // once per rendered frame and so already runs at the display's
                // refresh rate. A FrameAnimation here would tick at that rate
                // too, but it does not tick at all without a render loop,
                // which costs the whole offscreen test harness for no visible
                // gain — the easing interpolates between re-aims regardless.
                //
                // 8ms rather than 16 so retargeting stays finer than a frame
                // even at 165Hz. It only runs during a settle, ~150ms at a
                // time.
                //
                // Thresholds are in seconds, not frame counts: "six frames"
                // means 100ms at 60Hz and 36ms at 165Hz, so a frame-counted
                // budget silently gets stricter the better the monitor is.
                Timer {
                    id: settleToEnd
                    interval: 8
                    repeat: true
                    property real stableTime: 0
                    property real ranTime: 0
                    property real lastY: NaN
                    onTriggered: {
                        // User grabbed the list mid-settle — stop aiming and
                        // leave contentY alone so the first scroll is free.
                        if (msgList.userTookControl
                                || msgList.holdScroll
                                || msgList.landingFromSearch
                                || msgList.pendingJumpEventId > 0
                                || msgList.jumpTargetEventId > 0
                                || threadStore.suppressLandAtEnd) {
                            running = false
                            if (msgList.holdScroll || msgList.jumpTargetEventId > 0)
                                msgList.aimAtPendingJump()
                            msgList.landed = true
                            revealGuard.stop()
                            return
                        }
                        msgList.aimAtEnd()
                        var y = msgList.contentY
                        if (Math.abs(msgList.maxContentY - y) < 1 && y === lastY)
                            stableTime += interval / 1000.0
                        else
                            stableTime = 0
                        lastY = y
                        // Reveal as soon as it has held the bottom briefly,
                        // rather than waiting for the whole run to finish. The
                        // correction being hidden happens immediately;
                        // everything after it is sub-pixel, and waiting for a
                        // full stop left the conversation blank for noticeably
                        // longer than it needed to be.
                        if (stableTime >= 0.05) {
                            msgList.landed = true
                            // Landed on its own, so the backstop has nothing
                            // left to do — and firing anyway put a 3px jump
                            // into an already-visible conversation 700ms
                            // after it opened.
                            revealGuard.stop()
                        }
                        // Hold the bottom for a stretch rather than stopping
                        // at the first frame that looks settled. A bubble
                        // appended a moment ago has not necessarily been
                        // measured yet, so one good frame proves nothing —
                        // that is what left the view short of the bottom
                        // after a new message arrived.
                        ranTime += interval / 1000.0
                        if (stableTime >= 0.1 || ranTime > 1.5)
                            running = false
                    }
                    // Deliberately no onRunningChanged reveal: restart() is
                    // stop-then-start, so it fired with running == false and
                    // revealed the thread before a single aim had happened.
                    // `revealGuard` is the backstop, and it lands the view
                    // first.
                }

                // `positionViewAtEnd` alone is not the end of the scroll.
                // It rests the last delegate flush against the viewport
                // bottom, which leaves contentY a whole `bottomMargin` short
                // of `maxContentY` — so none of that margin is showing, the
                // delivery caption sits hard on the bottom edge, and the fade
                // that lives in the margin laps into it.
                //
                // Taking the remaining pixels explicitly is also what lets the
                // settle timer below ever finish: its "arrived" test compares
                // against maxContentY, which positionViewAtEnd never reaches,
                // so every open previously ran the full tick budget.
                readonly property bool jumpLock:
                    holdScroll || landingFromSearch
                    || pendingJumpEventId > 0 || jumpTargetEventId > 0
                    || threadStore.suppressLandAtEnd

                function jumpToEnd() {
                    // Search/reply jump owns the viewport — any end-aim during
                    // that window is the rubber-band back to the newest msg.
                    if (jumpLock)
                        return
                    positionViewAtEnd()
                    contentY = maxContentY
                }

                // The animated counterpart. Assignment only — deliberately no
                // `positionViewAtEnd`, because that writes contentY from C++
                // and a Behavior only sees writes made from QML. Calling it
                // here snapped the view on every settle tick and then eased
                // the few pixels left over, which is what the glide felt like
                // it was fighting.
                function glideToEnd() {
                    if (jumpLock)
                        return
                    contentY = maxContentY
                }

                // Every path that wants the bottom goes through here. There
                // must be exactly one writer of contentY at a time: while the
                // glide is in flight, an instant `jumpToEnd` from some other
                // path snaps the view forward and the easing curve then drags
                // it back to where it had got to — a jump followed by a
                // visible reversal.
                function aimAtEnd() {
                    if (jumpLock)
                        return
                    if (animateScroll)
                        glideToEnd()
                    else
                        jumpToEnd()
                }

                // Opening a thread cannot land on the bottom in one go — the
                // height of a conversation is an estimate until its delegates
                // exist, so the first aim is a few pixels out and the settle
                // timer corrects it a frame or two later. That correction was
                // visible as a small jump.
                //
                // So don't show the thread until it has stopped moving. The
                // wait is a couple of frames, and a short fade makes it read
                // as the conversation arriving rather than as a blink.
                property bool landed: false
                opacity: landed ? 1 : 0
                // A conversation that never settles must still be shown. This
                // is the ceiling on how long the fade can hold it back,
                // whatever the layout is doing.
                Timer {
                    id: revealGuard
                    interval: 700
                    onTriggered: {
                        // Search / jump-to-message owns the scroll position.
                        // Never jumpToEnd here — that was the late yank that
                        // undid a successful center on the hit ~half a second
                        // after open.
                        if (msgList.jumpLock) {
                            msgList.aimAtPendingJump()
                            msgList.landed = true
                            return
                        }
                        // User already scrolled during the settle window —
                        // reveal in place, do not yank back to the bottom.
                        if (msgList.userTookControl) {
                            msgList.landed = true
                            return
                        }
                        // Aim once more before showing it. Revealing blindly
                        // on a timer put the view on screen still thousands of
                        // pixels from the bottom on a long thread — the exact
                        // failure the fade exists to prevent, just later.
                        msgList.jumpToEnd()
                        msgList.landed = true
                    }
                }
                Behavior on opacity {
                    NumberAnimation {
                        // Only the fade *in* is animated. Fading out took
                        // 120ms during which the incoming thread was still on
                        // screen and still landing — measured on one thread it
                        // travelled 14760px while partly visible, in two jumps
                        // of 3141 and 10154px. That was the open jolt: not the
                        // settle being visible, but the previous thread's fade
                        // keeping the view lit while the next one searched for
                        // its bottom.
                        //
                        // A duration rather than `enabled`, because the
                        // animation reads its duration when it starts — after
                        // `landed` has already changed — where a binding on
                        // `enabled` races the opacity binding it guards.
                        duration: msgList.landed ? Theme.animFast : 0
                        easing.type: Easing.OutCubic
                    }
                }

                function landAtEnd() {
                    if (jumpLock)
                        return
                    settleToEnd.stableTime = 0
                    settleToEnd.ranTime = 0
                    settleToEnd.lastY = NaN
                    settleToEnd.restart()
                    // The first aim honours the mode too, or the glide
                    // starts with the very snap it exists to avoid.
                    aimAtEnd()
                }

                // Same landing, but glided rather than snapped — for a message
                // arriving while you are already at the bottom, where the jump
                // is the thing that reads as jarring. Opening a thread stays
                // instant: animating that would scroll the whole history past
                // you every time you switch conversation.
                //
                // The eased scroll is a Behavior, not an animation with a
                // fixed target, precisely because the target moves: the new
                // bubble's height is not known when the scroll starts, so
                // `jumpToEnd` gets re-applied by the settle timer above and
                // each re-application retargets the same easing curve instead
                // of restarting a new one. That is what keeps it from
                // stuttering as the layout firms up.
                property bool animateScroll: false
                Behavior on contentY {
                    enabled: msgList.animateScroll
                    // Same duration as the bubble's own arrival, so the
                    // thread rising and the message fading up into place are
                    // one movement rather than two of different lengths.
                    NumberAnimation {
                        duration: Theme.animArrive
                        easing.type: Easing.OutCubic
                    }
                }
                Timer {
                    id: followRelease
                    interval: Theme.animArrive + 400
                    onTriggered: msgList.animateScroll = false
                }

                function followToEnd() {
                    if (jumpLock)
                        return
                    animateScroll = true
                    followRelease.restart()
                    landAtEnd()
                }

                // Stick to the newest message as it arrives, but only when the
                // user is already near the bottom, so scrolling back through
                // history isn't yanked away when a message lands.
                //
                // Not `smoothScrollToEnd`: that routes through the eased
                // chase, which cannot track this list (see `smoothWheel`), so
                // an arriving message silently failed to scroll into view.
                onCountChanged: Qt.callLater(function () {
                    if (justOpened) {
                        justOpened = false
                        // Opening from search: jump handler positions the
                        // hit. landAtEnd here was overwriting that scroll.
                        if (jumpLock) {
                            aimAtPendingJump()
                            return
                        }
                        landAtEnd()
                        return
                    }
                    if (jumpLock) {
                        aimAtPendingJump()
                        return
                    }
                    // Scrolled back through history? Leave the view alone.
                    if (maxContentY - contentY < height * 0.5)
                        followToEnd()
                })
                // On first load / thread switch, jump without animating.
                Component.onCompleted: Qt.callLater(landAtEnd)
                // Opening a thread should land at the bottom instantly.
                // Without this the animated follow below runs on the reload,
                // so a long conversation visibly scrolled itself down.
                property bool justOpened: false
                // Images load asynchronously, so when a thread opens their
                // height is still 0 and "scroll to the end" lands short.
                // Keep re-pinning while content grows, until the user
                // scrolls for themselves.
                property bool pinBottom: false
                // Latched the moment the user takes the scroll. Distinct from
                // pinBottom because a contentHeight change can schedule
                // `Qt.callLater(aimAtEnd)` *before* the wheel/touchpad event
                // clears the pin — and that deferred aim was what made the
                // first scroll-up on open feel resisted (one yank back to
                // the bottom, then free). Once set, no open-path re-aim is
                // allowed until the next thread switch.
                property bool userTookControl: false
                // Only re-pin while the view is still down at the bottom.
                // Scrolling away is itself the signal to stop, which covers
                // input paths that raise no drag, flick or wheel signal.
                // No "already near the bottom" test here any more. It was
                // meant to stop a late image yanking the view, but that is
                // what `onUserScrolled` covers — and because the estimate can
                // be thousands of pixels out on open, the guard's real effect
                // was to abandon the pin exactly when it was needed and leave
                // the thread stranded mid-history.
                //
                // Past that window, growth still has to be followed — a
                // Delivered or Read caption appears under the last bubble
                // after the fact and makes the content taller beneath a view
                // already resting on the bottom. Nothing followed it, so the
                // receipt shoved the conversation up in a single frame.
                // Glide instead, so it reads like the new-message animation.
                property real lastMaxContentY: 0
                onContentHeightChanged: {
                    // The bottom before this growth. `atBottom` can't answer
                    // it: that is only recomputed when contentY moves, and
                    // what moved here is contentHeight.
                    var wasAtBottom = (lastMaxContentY - contentY) < 4
                    var delta = maxContentY - lastMaxContentY
                    lastMaxContentY = maxContentY
                    // Jump-to-message: never re-pin to the end while heights
                    // thrash. Re-center on every height change for the whole
                    // hold window — Flickable clamps contentY when maxContentY
                    // shrinks, which is the rubber-band-to-bottom without any
                    // end-aim call.
                    if (jumpLock) {
                        if (jumpTargetEventId > 0 || pendingJumpEventId > 0)
                            Qt.callLater(aimAtPendingJump)
                        return
                    }
                    if (pinBottom && !userTookControl) {
                        // Re-check both flags inside the deferred call: the
                        // user can take control between schedule and fire.
                        Qt.callLater(function () {
                            if (msgList.jumpLock)
                                return
                            if (msgList.pinBottom && !msgList.userTookControl)
                                msgList.aimAtEnd()
                        })
                        return
                    }
                    if (userTookControl)
                        return
                    if (!landed || !wasAtBottom || delta === 0)
                        return
                    // Content getting *shorter* is never a message; it is the
                    // list revising its own estimate as delegates outside the
                    // viewport are built and discarded. Measured on a 1.5k
                    // message thread it corrected by 4670px a couple of
                    // seconds after opening.
                    //
                    // Returning here left a bottom-resting view stranded up to
                    // 123px above the bottom, and the next growth then glided
                    // it back down — the open jolt: a drift up, then a slide
                    // back. Follow the shrink in the same frame instead, so
                    // the bottom never moves out from under the view.
                    if (delta < 0) {
                        aimAtEnd()
                        return
                    }
                    // Two quite different kinds of growth arrive here.
                    //
                    // A caption band opening under the last bubble grows a
                    // pixel or two per frame, because its own height is
                    // animated. The smooth motion is already being supplied
                    // by that animation, so all this has to do is stay welded
                    // to the bottom while it happens; easing on top would lag
                    // behind its own layout.
                    //
                    // A whole bubble appearing arrives in one step, and that
                    // is the one that needs the eased glide.
                    //
                    // `!animateScroll` guards the fast case: a message
                    // delivered almost as soon as it is sent brings its
                    // caption in while the bubble's glide is still running,
                    // and switching to an instant pin there killed the glide
                    // and snapped the rest of the distance in one frame.
                    // Staying in the glide simply retargets the same easing
                    // curve, which is what it is built to absorb.
                    if (delta < 40 && !animateScroll) {
                        jumpToEnd()
                    } else {
                        followToEnd()
                    }
                }

                // The composer grows as you add lines with Shift+Enter, which
                // shortens this view. Without following, the conversation
                // stayed where it was and the newest message slid up out of
                // sight behind the growing pill. Glided, not jumped, so it
                // rides along with the composer's own height animation and
                // reads as one movement.
                //
                // Only when you were already at the bottom: someone reading
                // history should keep their place while they type.
                property bool atBottom: true
                onContentYChanged: {
                    atBottom = (maxContentY - contentY) < 4
                    // Near the top → page in older history from SQLite.
                    // Skip during jump hold — prepending rebuilds the model
                    // and restarts the end-aim fight.
                    if (jumpLock)
                        return
                    // Only when the *user* has scrolled up a list that
                    // actually overflows. A first page that fits the
                    // viewport keeps contentY ≈ originY forever, so the
                    // naive "near top" test paginated the entire archive
                    // (20k+ rows on busy threads) into memory on open —
                    // multi-second freezes that looked like "loading too
                    // many messages".
                    if (!landed || justOpened || pinBottom || !userTookControl)
                        return
                    if (contentHeight <= height + 80)
                        return
                    if (contentY < originY + 200
                            && count > 0
                            && count !== _olderRequestAt) {
                        _olderRequestAt = count
                        threadStore.loadOlderMessages()
                    }
                }
                onHeightChanged: {
                    if (jumpLock)
                        return
                    if (landed && atBottom)
                        followToEnd()
                }
                // Any user scroll input releases the bottom pin — otherwise
                // a slowly-loading image growing contentHeight yanks the
                // view back down under them. Also latches userTookControl so
                // a deferred aimAtEnd scheduled before this event cannot
                // still win the first scroll-up after open.
                onUserScrolled: {
                    userTookControl = true
                    pinBottom = false
                    settleToEnd.running = false
                    // Or the wheel would be easing too, which feels like drag.
                    animateScroll = false
                    followRelease.stop()
                    pinRelease.stop()
                    // Hand control back if the user scrolls during a search jump.
                    if (jumpLock) {
                        searchJumpRelease.stop()
                        releaseHoldScroll()
                        landed = true
                    }
                }

                // Stop re-pinning shortly after opening, so a slow image
                // can't yank the view long afterwards. Short on purpose:
                // onUserScrolled is the real release; this is only a
                // backstop for threads the user never touches.
                Timer {
                    id: pinRelease
                    interval: 900
                    onTriggered: msgList.pinBottom = false
                }
                Connections {
                    target: threadStore
                    function onPeerChanged() {
                        msgList.justOpened = true
                        msgList.animateScroll = false
                        msgList.userTookControl = false
                        followRelease.stop()
                        // Hidden until it has settled at the bottom; see
                        // `landed`. Only on a thread switch — a message
                        // arriving must never blank the conversation.
                        msgList.landed = false
                        // Search jump owns the scroll position — stop every
                        // land-at-end path so they cannot yank the view after
                        // positionViewAtIndex.
                        if (threadStore.suppressLandAtEnd) {
                            msgList.beginHoldScroll()
                            // Still a backstop so a failed jump does not leave
                            // the pane invisible forever (sets landed only).
                            revealGuard.restart()
                            return
                        }
                        msgList.releaseHoldScroll()
                        searchJumpRelease.stop()
                        msgList.pinBottom = true
                        pinRelease.restart()
                        revealGuard.restart()
                        msgList.landAtEnd()
                    }
                }

                delegate: Column {
                    id: msgDelegate
                    required property string body
                    required property bool outgoing
                    required property var reactions
                    required property bool mediaOnly
                    required property string image
                    required property string fileLabel
                    required property string kind
                    required property string sender
                    required property string senderAvatar
                    required property string senderInitials
                    required property string divider
                    // Extra space above the bubble when there was a real pause
                    // before it. Zero for a burst, so quick back-and-forth
                    // still stacks tightly. See _RUN_GAP_SECONDS.
                    required property int gapBefore
                    required property bool tail
                    required property string richBody
                    required property bool jumbo
                    required property int imageW
                    required property int imageH
                    // iMessage's own id, empty for MAP messages. Gates every
                    // action the context menu offers — none of them can name
                    // their target without it.
                    required property string guid
                    // "", "delivered" or "read". Only ever set on the last
                    // outgoing bubble; see _rebuild_messages.
                    //
                    // Not called `state`: every QML Item already has one,
                    // and shadowing it breaks the state machine silently.
                    required property string deliveryState
                    required property string deliveryStamp
                    // "6:24 PM". Drawn only in the Show Times gutter.
                    required property string timeStamp
                    // Earlier texts of an edited message, oldest first.
                    required property var edits
                    // Non-empty when this message replies to another.
                    required property string replyBody
                    // Guid of the parent message (paired with replyBody) so
                    // the quote can jump to the original on tap.
                    required property string replyGuid
                    // Store row id + search-hit highlight.
                    required property int eventId
                    required property bool highlight
                    // Collapsed by default: the current text is the one that
                    // counts, and history would otherwise pad every edited
                    // message forever.
                    property bool showEdits: false

                    // One soft scale pulse when this bubble becomes the
                    // jump target (search hit or reply-quote). `_didPulse`
                    // keeps scroll-away/back from replaying it until the
                    // highlight is cleared and set again.
                    property real rimScale: 1.0
                    property bool _didPulse: false
                    onHighlightChanged: {
                        if (highlight) {
                            if (!_didPulse) {
                                _didPulse = true
                                rimPop.restart()
                            }
                        } else {
                            _didPulse = false
                            rimScale = 1.0
                            rimPop.stop()
                        }
                    }
                    SequentialAnimation {
                        id: rimPop
                        NumberAnimation {
                            target: msgDelegate; property: "rimScale"
                            from: 1.0; to: 1.03
                            duration: 140
                            easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: msgDelegate; property: "rimScale"
                            to: 1.0
                            duration: 360
                            easing.type: Easing.OutCubic
                        }
                    }

                    // Gap from the window edge to the bubble's right wall.
                    // The delivery caption aligns to this too — and so, with
                    // Show Times on, does the slide that opens the gutter:
                    // everything anchored to the right moves as one because
                    // they all measure from here.
                    //
                    // Animated on the delegate rather than on the shared
                    // property, so each row eases independently and the
                    // conversation opens the gutter as a wave rather than a
                    // single hard shift.
                    property int sideMargin: 22 + (msgArea.showTimes
                                                   ? msgArea.timeGutter : 0)
                    Behavior on sideMargin {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }

                    // Driven by the view's `add` transition, so it only ever
                    // moves for a genuine insertion — never for a delegate
                    // being created because you scrolled it into view.
                    //
                    // A transform, not `y`: animating the layout position is
                    // what made this jitter before, because the eased contentY
                    // glide is moving the same bubble at the same time and the
                    // two rates fought. A transform is applied after layout,
                    // so the scroll maths never sees it and the two motions
                    // simply add.
                    property real riseOffset: 0
                    transform: Translate { y: msgDelegate.riseOffset }
                    // True only while the add transition above is running.
                    property bool freshlyAdded: false

                    // Media stands on its own: a photo is a rounded image
                    // with no bubble behind it, a sticker is bare and
                    // unrounded. Any accompanying text is its own bubble.
                    // Group threads keep a gutter on the left for the
                    // sender's avatar; 1:1 threads don't need one.
                    readonly property bool inGroup:
                        threadStore.peerIsGroup && !outgoing
                    readonly property int gutter: inGroup ? 40 : 0

                    readonly property bool isImage: kind === "image"
                    readonly property bool isSticker: kind === "sticker"
                    readonly property bool isMedia: isImage || isSticker
                    // True while the free-standing Image is actually drawable.
                    // Empty source or a failed decode must not leave a blank
                    // hole — the file-chip bubble falls back instead.
                    readonly property bool showMedia:
                        isMedia && image !== ""
                        && mediaItem.status !== Image.Error
                    // An emoji-only message is drawn big and bare. It still
                    // uses the bubble item for layout — only the painted
                    // background and the tail drop away.
                    readonly property bool isJumbo: jumbo && !isMedia

                    width: msgList.width
                    spacing: 0
                    // Padding rather than ListView spacing: spacing is one
                    // value for the whole list, and this has to vary per
                    // message.
                    topPadding: gapBefore

                    // Outline of a bubble, optionally with the tail merged
                    // into its bottom corner as part of the same closed path.
                    // Built for a right-hand bubble and mirrored through
                    // X() for incoming ones — mirroring reverses the winding,
                    // hence the flipped arc sweep flag.
                    function bubblePath(w0, h0, r0, hasTail, right, strokeW) {
                        // Inset by half the rim stroke. The stroke is centred
                        // on the outline, so an outline spanning y ∈ [0, h]
                        // puts fill on rows 0..h-1 and leaves the stroke's
                        // outer half alone on row h — over the background,
                        // where it reads as a pale grey line under every
                        // bubble. The top edge escaped it only because its
                        // stroke fell on row -1, outside the item. Building
                        // the outline half a pixel in makes all four edges
                        // land inside the fill, and symmetric.
                        //
                        // `strokeW` must match ShapePath.strokeWidth: the
                        // search/reply highlight uses 2px, and leaving the
                        // inset at 0.5 clipped the outer half of that stroke
                        // against the Shape bounds — the pixel crumbs on the
                        // rim.
                        const sw = (strokeW > 0) ? strokeW : 1
                        const inset = sw * 0.5
                        const w = w0 - inset * 2
                        const h = h0 - inset * 2
                        // A single-line bubble is shorter than twice the
                        // corner radius. Unclamped, the side segment below
                        // runs *upwards*, the outline crosses itself, and the
                        // semi-transparent rim stroke paints over itself at
                        // each end — the pale specks that only ever appeared
                        // on one-line messages.
                        const r = Math.min(r0, h / 2)
                        const out = 6.5         // how far the tip reaches out
                        // Both axes shift by the inset; X also mirrors for
                        // incoming bubbles, which reverses the arc winding.
                        const X = x => (right ? x : (w - x)) + inset
                        const Y = y => y + inset
                        const s = right ? 1 : 0
                        const arc = (x, y) =>
                            ` A ${r} ${r} 0 0 ${s} ${X(x)} ${Y(y)}`
                        const L = (x, y) => ` L ${X(x)} ${Y(y)}`
                        const C = (x1, y1, x2, y2, x, y) =>
                            ` C ${X(x1)} ${Y(y1)}, ${X(x2)} ${Y(y2)}, ` +
                            `${X(x)} ${Y(y)}`

                        let p = `M ${X(r)} ${Y(0)}` + L(w - r, 0) + arc(w, r)
                        if (hasTail) {
                            // The tail replaces the bottom corner rather than
                            // hanging off a straight section of wall: the side
                            // flares directly into the point, then hooks back
                            // underneath. Keeping any straight run before the
                            // flare is what made the old one read as a blunt
                            // flap instead of a tail.
                            // ...but clamping r alone isn't enough here. At
                            // r == h/2 the top arc already ends at y == r,
                            // while the tail wants to start at h - r*1.05,
                            // which is *above* it — so the wall doubled back
                            // and the rim stroke overpainted itself again,
                            // leaving the speck on exactly the one-line
                            // bubbles that have a tail. Never start the flare
                            // higher than where the corner left off.
                            // The tip is deliberately not a corner: it ends a
                            // hair above the baseline and the return curve's
                            // first control point sits below and *outside* it,
                            // so the two curves meet through a small arc
                            // instead of a cusp. Pulling that control point
                            // back toward the wall is what sharpens it into a
                            // spike.
                            p += L(w, Math.max(r, h - r * 0.85))
                               + C(w, h - r * 0.28,
                                   w + out * 0.45, h - r * 0.30,
                                   w + out, h - 0.9)
                               + C(w + out * 0.55, h + 0.9,
                                   w - 4, h,
                                   w - r * 0.78, h)
                        } else {
                            p += L(w, h - r) + arc(w - r, h)
                        }
                        return p + L(r, h) + arc(0, h - r)
                                 + L(0, r) + arc(r, 0) + " Z"
                    }

                    // Fading is handled by a single gradient mask over the
                    // whole list (see msgList.layer below), so a tall bubble
                    // dissolves progressively from its top edge instead of
                    // dimming as one flat block.

                    // Time divider only where the conversation paused.
                    Item {
                        width: parent.width
                        height: divider === "" ? 0 : 30
                        visible: divider !== ""
                        Text {
                            anchors.centerIn: parent
                            text: divider
                            color: Theme.textDim
                            font.pixelSize: 11
                        }
                    }

                    // Sender label — group chats only, so you can tell who
                    // said what. Hidden in 1:1 threads where it's obvious.
                    Item {
                        width: parent.width
                        height: showSender ? 15 : 0
                        visible: showSender
                        readonly property bool showSender:
                            sender !== "" && !outgoing
                        Text {
                            // Indented past the bubble's corner curve so the
                            // name starts where the straight top edge does,
                            // rather than floating out over the round corner.
                            x: 16 + gutter + Theme.radiusBubble * 0.8
                            text: sender
                            color: Theme.textDim
                            font.pixelSize: 10
                        }
                    }

                    // The message being replied to, quoted above this one and
                    // on the same side of the thread. Without it a reply is
                    // visually identical to an ordinary message, which is
                    // indistinguishable from replying being broken.
                    //
                    // Tappable when we know the parent's guid: jumps to that
                    // bubble the same way a search hit does.
                    Item {
                        width: msgList.width
                        height: msgDelegate.replyBody === ""
                                ? 0 : quoted.implicitHeight + 6
                        visible: height > 0

                        Row {
                            id: quoteRow
                            anchors.right: msgDelegate.outgoing
                                           ? parent.right : undefined
                            anchors.left: msgDelegate.outgoing
                                          ? undefined : parent.left
                            anchors.rightMargin: msgDelegate.sideMargin + 6
                            anchors.leftMargin: 16 + msgDelegate.gutter + 6
                            spacing: 5
                            // Hoverable only when the parent guid is known.
                            // Soft white lift — not a blue underlined link;
                            // the accent rim on the jumped-to bubble is the
                            // real confirmation.
                            readonly property bool quoteLit:
                                quoteHover.hovered
                                && msgDelegate.replyGuid !== ""

                            // A short rule standing in for iOS's curved
                            // connector, marking the quote as attached to the
                            // bubble below rather than floating free.
                            Rectangle {
                                width: 2
                                height: quoted.implicitHeight
                                radius: 1
                                color: Theme.textDim
                                // Brightens with the quote on hover so the
                                // whole reply affordance reads as one control.
                                opacity: quoteRow.quoteLit ? 0.7 : 0.45
                                Behavior on opacity {
                                    NumberAnimation {
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }

                            Text {
                                id: quoted
                                // Width is the text itself (capped), not the
                                // full thread — handlers live on this item so
                                // only the preview is hoverable/clickable,
                                // not the empty rest of the row.
                                width: Math.min(implicitWidth,
                                                msgList.width * 0.5)
                                text: msgDelegate.replyBody
                                // Rich when the parent had an inline sticker
                                // (U+F00A → <img> in the snippet); plain
                                // otherwise so a body with "<" isn't parsed
                                // as markup.
                                textFormat: msgDelegate.replyBody.indexOf(
                                                "<img") >= 0
                                            ? Text.RichText : Text.PlainText
                                // Elide only works on plain text; rich quotes
                                // with a sticker are short enough in practice.
                                elide: textFormat === Text.PlainText
                                       ? Text.ElideRight : Text.ElideNone
                                maximumLineCount: 2
                                wrapMode: Text.Wrap
                                // Resting dim caption. On hover, lift toward
                                // full text colour — a soft white wash, not
                                // a blue link.
                                color: quoteRow.quoteLit
                                       ? Theme.text : Theme.textDim
                                opacity: quoteRow.quoteLit ? 1.0 : 0.85
                                // Must match models._REPLY_PX so inline
                                // stickers in the quote are text-tall here.
                                font.pixelSize: 11
                                Behavior on color {
                                    ColorAnimation {
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }
                                Behavior on opacity {
                                    NumberAnimation {
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }

                                HoverHandler {
                                    id: quoteHover
                                    enabled: msgDelegate.replyGuid !== ""
                                    cursorShape: Qt.PointingHandCursor
                                }
                                TapHandler {
                                    enabled: msgDelegate.replyGuid !== ""
                                    onTapped: threadStore.jumpToGuid(
                                                  msgDelegate.replyGuid)
                                }
                            }
                        }
                    }

                    // Previous versions of an edited message, revealed by the
                    // "Edited" link in the caption. Drawn as real (tailless)
                    // bubbles, faded, oldest first above the current text —
                    // history that still reads as the same conversation,
                    // not as plain italic notes.
                    //
                    // Height is animated on the column so expanding/collapsing
                    // pushes the thread above smoothly (ListView contentHeight
                    // tracks it; bottom-follow keeps the current message in
                    // view). Individual bubbles fan in/out with a stagger
                    // from the current message upward.
                    Column {
                        id: editHistory
                        width: parent.width
                        spacing: 3
                        // Clip so the height collapse hides children while
                        // they are still in the tree for the close animation.
                        clip: true
                        readonly property bool wantOpen:
                            msgDelegate.showEdits
                            && msgDelegate.edits.length > 0
                        // Keep the model mounted whenever there is history so
                        // closing can animate; only the height collapses to 0.
                        visible: msgDelegate.edits.length > 0
                        height: wantOpen ? implicitHeight : 0
                        Behavior on height {
                            NumberAnimation {
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                        }

                        Repeater {
                            id: editRepeater
                            // Always the full list when non-empty: toggling
                            // the model with showEdits would destroy items
                            // on close and kill the fan-down animation.
                            model: msgDelegate.edits

                            Item {
                                id: histItem
                                required property int index
                                required property string modelData
                                width: msgList.width
                                height: histBubble.height + 4

                                // Fan offset: starts below (toward the current
                                // message) and rises into place on open.
                                property real fanY: 10
                                property real fanOpacity: 0
                                opacity: fanOpacity
                                transform: Translate { y: histItem.fanY }

                                // Stagger from the current message upward:
                                // the version closest to the live bubble
                                // (highest index) moves first; oldest last.
                                readonly property int openDelay:
                                    Math.max(0, (editRepeater.count - 1 - index)
                                             * 40)
                                // Close fans down from the top: oldest first.
                                readonly property int closeDelay:
                                    Math.max(0, index * 30)

                                function playOpen() {
                                    closeAnim.stop()
                                    openAnim.restart()
                                }
                                function playClose() {
                                    openAnim.stop()
                                    closeAnim.restart()
                                }

                                SequentialAnimation {
                                    id: openAnim
                                    PauseAnimation {
                                        duration: histItem.openDelay
                                    }
                                    ParallelAnimation {
                                        NumberAnimation {
                                            target: histItem
                                            property: "fanOpacity"
                                            to: 0.5
                                            duration: Theme.animArrive
                                            easing.type: Easing.OutCubic
                                        }
                                        NumberAnimation {
                                            target: histItem
                                            property: "fanY"
                                            to: 0
                                            duration: Theme.animArrive
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                }
                                SequentialAnimation {
                                    id: closeAnim
                                    PauseAnimation {
                                        duration: histItem.closeDelay
                                    }
                                    ParallelAnimation {
                                        NumberAnimation {
                                            target: histItem
                                            property: "fanOpacity"
                                            to: 0
                                            duration: Theme.animBase
                                            easing.type: Easing.OutCubic
                                        }
                                        NumberAnimation {
                                            target: histItem
                                            property: "fanY"
                                            to: 10
                                            duration: Theme.animBase
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                }

                                Connections {
                                    target: editHistory
                                    function onWantOpenChanged() {
                                        if (editHistory.wantOpen)
                                            histItem.playOpen()
                                        else
                                            histItem.playClose()
                                    }
                                }
                                Component.onCompleted: {
                                    // Mid-open rebuild is rare; still land
                                    // in the correct resting state.
                                    if (editHistory.wantOpen) {
                                        fanOpacity = 0.5
                                        fanY = 0
                                    }
                                }

                                // Tailless bubble matching the live one on
                                // this side — same path helper, same fill,
                                // no tail so history doesn't read as a
                                // second live message.
                                Item {
                                    id: histBubble
                                    anchors.right: msgDelegate.outgoing
                                                   ? parent.right : undefined
                                    anchors.left: msgDelegate.outgoing
                                                  ? undefined : parent.left
                                    anchors.rightMargin: msgDelegate.sideMargin
                                    anchors.leftMargin: 16 + msgDelegate.gutter
                                    readonly property int padH: 28
                                    readonly property int padV: 16
                                    width: Math.min(
                                        histText.implicitWidth + padH,
                                        msgList.width * 0.68)
                                    height: histText.implicitHeight + padV

                                    Shape {
                                        anchors.fill: parent
                                        preferredRendererType: Shape.CurveRenderer
                                        ShapePath {
                                            fillColor: msgDelegate.outgoing
                                                       ? "transparent"
                                                       : Theme.bubbleIn
                                            fillGradient: msgDelegate.outgoing
                                                          ? histGrad : null
                                            strokeColor: Theme.bubbleRim
                                            strokeWidth: 1
                                            joinStyle: ShapePath.RoundJoin
                                            capStyle: ShapePath.RoundCap
                                            PathSvg {
                                                path: msgDelegate.bubblePath(
                                                    histBubble.width,
                                                    histBubble.height,
                                                    Theme.radiusBubble,
                                                    false,
                                                    msgDelegate.outgoing,
                                                    1)
                                            }
                                        }
                                        Shapes.LinearGradient {
                                            id: histGrad
                                            x1: 0; y1: 0
                                            x2: 0; y2: histBubble.height
                                            GradientStop {
                                                position: 0.0
                                                color: Theme.bubbleOutTop
                                            }
                                            GradientStop {
                                                position: 1.0
                                                color: Theme.bubbleOutBot
                                            }
                                        }
                                    }

                                    Text {
                                        id: histText
                                        anchors {
                                            left: parent.left
                                            right: parent.right
                                            verticalCenter: parent.verticalCenter
                                            leftMargin: histBubble.padH / 2
                                            rightMargin: histBubble.padH / 2
                                        }
                                        text: histItem.modelData
                                        wrapMode: Text.Wrap
                                        font.pixelSize: Math.round(
                                            13 * Theme.fontScale)
                                        color: msgDelegate.outgoing
                                               ? "white" : Theme.bubbleInText
                                    }
                                }
                            }
                        }
                    }

                    Item {
                        id: msgRow
                        width: parent.width
                        height: content.height + 4

                        // Right-click target, tracking whichever element is
                        // actually showing — a photo has no bubble, and an
                        // earlier version anchored to the bubble alone, which
                        // is why attachments couldn't be reacted to.
                        //
                        // Declared before the visuals so it sits beneath
                        // them; nothing above accepts right-clicks, so the
                        // events reach it either way.
                        MouseArea {
                            id: bubbleHit
                            anchors.fill: msgDelegate.showMedia ? mediaItem : bubble
                            // Generous: the bubble is sized tight to its text
                            // and a few px either way should still count.
                            anchors.margins: -4
                            // Not gated on the guid any more: Copy works
                            // without one, so a MAP message still has a menu
                            // worth opening.
                            enabled: msgDelegate.guid !== ""
                                     || msgDelegate.body !== ""
                            acceptedButtons: Qt.RightButton
                            onClicked: (mouse) => {
                                bubbleMenu.picking = false
                                bubbleMenu.x = mouse.x
                                bubbleMenu.y = mouse.y - bubbleMenu.height - 4
                                bubbleMenu.open()
                            }
                        }

                        // Actions for one message. A plain Popup rather than
                        // a Menu: the tapback row has to be a row of
                        // individually-clickable emoji, and a MenuItem
                        // consumes the click before its contents ever see it.
                        Popup {
                            id: bubbleMenu
                            padding: 6
                            // Deliberately not modal, and no
                            // CloseOnPressOutside. Both make the overlay
                            // swallow the press that dismisses the menu,
                            // which meant right-clicking a *second* bubble
                            // only closed the first one's menu — every other
                            // right-click did nothing, and the messages that
                            // landed on the dead beat looked un-reactable.
                            // The page's own press handler closes this
                            // instead (see `dismissMenu`), leaving the press
                            // free to reach the bubble under it.
                            modal: false
                            dim: false
                            closePolicy: Popup.CloseOnEscape
                            // Menu opens above the bubble; grow from that edge.
                            transformOrigin: Item.Bottom

                            // Showing the full emoji picker rather than the
                            // six classic tapbacks.
                            property bool picking: false

                            onOpened: msgArea.openMenu = bubbleMenu
                            onClosed: {
                                picking = false
                                if (msgArea.openMenu === bubbleMenu)
                                    msgArea.openMenu = null
                            }

                            enter: Transition {
                                ParallelAnimation {
                                    NumberAnimation {
                                        property: "opacity"; from: 0; to: 1
                                        duration: Theme.animFast
                                    }
                                    NumberAnimation {
                                        property: "scale"
                                        from: Theme.popScale; to: 1
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }
                            exit: Transition {
                                ParallelAnimation {
                                    NumberAnimation {
                                        property: "opacity"; to: 0
                                        duration: Theme.animMicro
                                    }
                                    NumberAnimation {
                                        property: "scale"; to: Theme.popScale
                                        duration: Theme.animMicro
                                    }
                                }
                            }

                            background: Rectangle {
                                color: Theme.dark ? Qt.rgba(0.17, 0.17, 0.19, 0.98)
                                                  : Qt.rgba(1, 1, 1, 0.98)
                                radius: 12
                                border.width: 1
                                border.color: Theme.separator
                            }

                            contentItem: Column {
                                id: menuBody
                                spacing: 2
                                // Match the widest section (tapback row is
                                // seven 32px chips + gaps = 236). Action rows
                                // used a fixed 190, so their hover fill
                                // stopped short of the menu edge under the
                                // reactions. Computed, not taken from child
                                // implicitWidth — binding width to a child
                                // that also binds to this width cycles.
                                readonly property int menuWidth:
                                    bubbleMenu.picking
                                        ? Math.max(190, 6 * 32)
                                        : (msgDelegate.guid !== ""
                                           ? (7 * 32 + 6 * 2) : 190)
                                width: menuWidth
                                Behavior on width {
                                    NumberAnimation {
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }

                                // The six classic tapbacks, in a row, the way
                                // iOS presents them, with a "more" button on
                                // the end for everything else. Height/opacity
                                // crossfade with the full picker below so
                                // classic ↔ more is not a hard swap.
                                Row {
                                    id: reactionRow
                                    spacing: 2
                                    // Same rule as the verbs below: a tapback
                                    // names its target by guid, so a message
                                    // without one can't carry a reaction.
                                    readonly property bool shown:
                                        msgDelegate.guid !== ""
                                        && !bubbleMenu.picking
                                    height: shown ? 32 : 0
                                    opacity: shown ? 1 : 0
                                    clip: true
                                    visible: height > 0.5 || opacity > 0.01
                                    Behavior on height {
                                        NumberAnimation {
                                            duration: Theme.animFast
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                    Behavior on opacity {
                                        NumberAnimation { duration: Theme.animFast }
                                    }
                                    Repeater {
                                        model: [
                                            { emoji: "❤️", kind: "Heart" },
                                            { emoji: "👍", kind: "Like" },
                                            { emoji: "👎", kind: "Dislike" },
                                            { emoji: "😂", kind: "Laugh" },
                                            { emoji: "‼️", kind: "Emphasize" },
                                            { emoji: "❓", kind: "Question" },
                                        ]
                                        Rectangle {
                                            required property var modelData
                                            width: 32; height: 32
                                            radius: 16
                                            color: tapHover.hovered
                                                   ? Qt.rgba(1, 1, 1, 0.12)
                                                   : "transparent"
                                            Behavior on color {
                                                ColorAnimation { duration: Theme.animFast }
                                            }

                                            Text {
                                                anchors.centerIn: parent
                                                text: parent.modelData.emoji
                                                font.pixelSize: 17
                                                scale: tapHover.hovered
                                                       ? Theme.hoverScale : 1.0
                                                Behavior on scale {
                                                    NumberAnimation {
                                                        duration: Theme.animFast
                                                        easing.type: Easing.OutCubic
                                                    }
                                                }
                                            }

                                            HoverHandler { id: tapHover }
                                            TapHandler {
                                                onTapped: {
                                                    // A verb, not the emoji:
                                                    // these six have real
                                                    // tapback types on the
                                                    // wire, and sending them
                                                    // as arbitrary emoji
                                                    // would show up on the
                                                    // phone as the iOS 18
                                                    // kind instead of the
                                                    // classic one.
                                                    threadStore.react(
                                                        msgDelegate.guid,
                                                        parent.modelData.kind)
                                                    bubbleMenu.close()
                                                }
                                            }
                                        }
                                    }

                                    // Anything that isn't one of the six.
                                    Rectangle {
                                        width: 32; height: 32
                                        radius: 16
                                        color: moreHover.hovered
                                               ? Qt.rgba(1, 1, 1, 0.12)
                                               : "transparent"
                                        Behavior on color {
                                            ColorAnimation { duration: Theme.animFast }
                                        }
                                        Text {
                                            anchors.centerIn: parent
                                            text: "＋"
                                            color: Theme.textDim
                                            font.pixelSize: 15
                                            scale: moreHover.hovered
                                                   ? Theme.hoverScale : 1.0
                                            Behavior on scale {
                                                NumberAnimation {
                                                    duration: Theme.animFast
                                                    easing.type: Easing.OutCubic
                                                }
                                            }
                                        }
                                        HoverHandler { id: moreHover }
                                        TapHandler {
                                            onTapped: bubbleMenu.picking = true
                                        }
                                    }
                                }

                                // The full picker, in place of the six.
                                // iMessage carries any emoji as a tapback
                                // (iOS 18+) and we could already *receive*
                                // those; this is the half that sends them.
                                Column {
                                    id: emojiPicker
                                    spacing: 4
                                    width: menuBody.width
                                    readonly property bool shown:
                                        msgDelegate.guid !== ""
                                        && bubbleMenu.picking
                                    // implicitHeight is 0 while empty; once
                                    // shown, measure the full column so the
                                    // height ease has a real target.
                                    height: shown ? implicitHeight : 0
                                    opacity: shown ? 1 : 0
                                    clip: true
                                    visible: height > 0.5 || opacity > 0.01
                                    Behavior on height {
                                        NumberAnimation {
                                            duration: Theme.animFast
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                    Behavior on opacity {
                                        NumberAnimation { duration: Theme.animFast }
                                    }

                                    // Whatever `query` matches, or the
                                    // hand-picked openers before it is typed
                                    // in. Recomputed on demand rather than
                                    // bound, so the list survives the field
                                    // being cleared.
                                    property var choices: []

                                    function refresh() {
                                        choices = query.text.length > 0
                                                  ? emoji.search(query.text)
                                                  : emoji.popular(24)
                                    }

                                    onShownChanged: {
                                        if (!shown)
                                            return
                                        query.text = ""
                                        refresh()
                                        query.forceActiveFocus()
                                    }

                                    TextField {
                                        id: query
                                        width: parent.width
                                        height: 26
                                        placeholderText: "Search emoji"
                                        color: Theme.text
                                        font.pixelSize: 12
                                        leftPadding: 8
                                        background: Rectangle {
                                            radius: 7
                                            color: Qt.rgba(1, 1, 1, 0.08)
                                        }
                                        onTextChanged: emojiPicker.refresh()
                                        // Enter sends the first match, so a
                                        // search that already found what you
                                        // meant needn't be aimed at.
                                        Keys.onReturnPressed: {
                                            if (emojiPicker.choices.length > 0)
                                                msgArea.sendReaction(
                                                    msgDelegate.guid,
                                                    emojiPicker.choices[0].emoji,
                                                    bubbleMenu)
                                        }
                                        Keys.onEscapePressed: bubbleMenu.picking = false
                                    }

                                    Grid {
                                        columns: 6
                                        spacing: 0
                                        width: parent.width
                                        Repeater {
                                            model: emojiPicker.choices
                                            Rectangle {
                                                required property var modelData
                                                width: 32; height: 32
                                                radius: 16
                                                color: pickHover.hovered
                                                       ? Qt.rgba(1, 1, 1, 0.12)
                                                       : "transparent"
                                                Behavior on color {
                                                    ColorAnimation { duration: Theme.animFast }
                                                }
                                                Text {
                                                    anchors.centerIn: parent
                                                    text: parent.modelData.emoji
                                                    font.pixelSize: 17
                                                    scale: pickHover.hovered
                                                           ? Theme.hoverScale : 1.0
                                                    Behavior on scale {
                                                        NumberAnimation {
                                                            duration: Theme.animFast
                                                            easing.type: Easing.OutCubic
                                                        }
                                                    }
                                                }
                                                HoverHandler { id: pickHover }
                                                TapHandler {
                                                    onTapped: msgArea.sendReaction(
                                                        msgDelegate.guid,
                                                        parent.modelData.emoji,
                                                        bubbleMenu)
                                                }
                                            }
                                        }
                                    }

                                    Text {
                                        visible: emojiPicker.choices.length === 0
                                        width: parent.width
                                        text: "No emoji matches “" + query.text + "”"
                                        elide: Text.ElideRight
                                        color: Theme.textDim
                                        font.pixelSize: 11
                                    }
                                }

                                Rectangle {
                                    width: menuBody.width
                                    height: 1
                                    color: Theme.separator
                                }

                                Repeater {
                                    model: [
                                        // Copy is the one action that names no
                                        // target on the wire, so unlike the
                                        // rest it works on a MAP message too.
                                        { label: "Copy", act: "copy",
                                          need: false, needGuid: false },
                                        { label: "Reply", act: "reply",
                                          need: false, needGuid: true },
                                        { label: "Edit", act: "edit",
                                          need: true, needGuid: true },
                                        { label: "Undo Send", act: "unsend",
                                          need: true, needGuid: true },
                                        // The one entry that isn't about this
                                        // message. It belongs here anyway:
                                        // right-clicking a bubble is the
                                        // obvious way to reach it, and this
                                        // menu covers the bubble the
                                        // background one would have handled.
                                        { label: msgArea.showTimes
                                                 ? "Hide Times" : "Show Times",
                                          act: "times", need: false,
                                          needGuid: false, always: true },
                                    ]
                                    Rectangle {
                                        required property var modelData
                                        // Edit and Undo Send only apply to
                                        // our own messages. The rest need a
                                        // guid to name their target —
                                        // offering Reply without one is what
                                        // made replying look broken: it left
                                        // the composer in plain mode and the
                                        // "reply" went out as an ordinary
                                        // message.
                                        readonly property bool allowed:
                                            modelData.always === true ||
                                            (modelData.needGuid
                                             ? msgDelegate.guid !== ""
                                             : msgDelegate.body !== "")
                                            && (!modelData.need
                                                || msgDelegate.outgoing)
                                        // Full menu width so the hover fill
                                        // matches the tapback row above.
                                        width: menuBody.width
                                        height: visible ? 30 : 0
                                        visible: allowed
                                        radius: 7
                                        color: rowHover.hovered
                                               ? Qt.rgba(1, 1, 1, 0.10)
                                               : "transparent"
                                        Behavior on color {
                                            ColorAnimation { duration: Theme.animFast }
                                        }

                                        Text {
                                            anchors.verticalCenter: parent.verticalCenter
                                            anchors.left: parent.left
                                            anchors.leftMargin: 10
                                            // Slight indent on hover — reads
                                            // as the row pressing in without
                                            // scaling the whole menu shell.
                                            transform: Translate {
                                                x: rowHover.hovered ? 2 : 0
                                                Behavior on x {
                                                    NumberAnimation {
                                                        duration: Theme.animFast
                                                        easing.type: Easing.OutCubic
                                                    }
                                                }
                                            }
                                            text: parent.modelData.label
                                            color: Theme.text
                                            font.pixelSize: 12
                                        }

                                        HoverHandler { id: rowHover }
                                        TapHandler {
                                            onTapped: {
                                                var a = parent.modelData.act
                                                if (a === "times")
                                                    msgArea.showTimes =
                                                        !msgArea.showTimes
                                                else if (a === "copy")
                                                    // Selection wins when
                                                    // there is one — copying
                                                    // the whole message after
                                                    // the user deliberately
                                                    // highlighted part of it
                                                    // throws their work away.
                                                    threadStore.copyText(
                                                        msgText.selectedText
                                                        || msgDelegate.body)
                                                else if (a === "reply")
                                                    composer.beginReply(
                                                        msgDelegate.guid,
                                                        msgDelegate.body)
                                                else if (a === "edit")
                                                    composer.beginEdit(
                                                        msgDelegate.guid,
                                                        msgDelegate.body)
                                                else
                                                    threadStore.unsendMessage(
                                                        msgDelegate.guid)
                                                bubbleMenu.close()
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // ---- media: no bubble, stands on its own --------
                        // Skeleton holds the reserved footprint while the
                        // decode finishes; the photo fades in over it. Size
                        // stays fixed from import metrics so scroll settle
                        // is not re-fought with a height animation.
                        Rectangle {
                            id: mediaSkeleton
                            anchors {
                                right: outgoing ? parent.right : undefined
                                left: outgoing ? undefined : parent.left
                                rightMargin: msgDelegate.sideMargin
                                leftMargin: 16 + gutter
                            }
                            y: 2
                            width: mediaItem.width
                            height: mediaItem.height
                            radius: isSticker ? 0 : 16
                            color: Qt.rgba(1, 1, 1, 0.06)
                            // Photos only — a flat plate under a transparent
                            // sticker looks like a broken download. Opacity
                            // (not bare visible) so the plate eases out as
                            // the decode lands rather than vanishing under
                            // the fade-in.
                            readonly property bool waiting:
                                isImage && image !== ""
                                && mediaItem.status !== Image.Ready
                                && mediaItem.status !== Image.Error
                            opacity: waiting ? 1 : 0
                            visible: opacity > 0.01
                            Behavior on opacity {
                                NumberAnimation { duration: Theme.animFast }
                            }
                        }

                        Image {
                            id: mediaItem
                            // Hide on empty source or decode failure so the
                            // file-chip bubble below can take over. Loading
                            // still reserves space via width/height below.
                            visible: isMedia && image !== ""
                                     && status !== Image.Error
                            source: isMedia && image !== "" ? image : ""
                            anchors {
                                right: outgoing ? parent.right : undefined
                                left: outgoing ? undefined : parent.left
                                rightMargin: msgDelegate.sideMargin
                                leftMargin: 16 + gutter
                            }
                            y: 2
                            // Sized from the dimensions recorded at import
                            // rather than from the decoded image. Waiting for
                            // the decode meant every photo was 0px tall until
                            // it arrived and then shoved the list around,
                            // which is what made image threads scroll badly.
                            // 4:3 is the fallback for anything unmeasured.
                            readonly property real aspect:
                                (imageW > 0 && imageH > 0) ? (imageH / imageW)
                                                           : 0.75
                            width: Math.min(imageW > 0 ? imageW : 300,
                                            msgList.width * 0.55,
                                            isSticker ? 180 : 300)
                            height: Math.round(width * aspect)
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            // Cached: decoding is capped at sourceSize, so
                            // entries are small, and re-decoding on every
                            // recycle made images blink during scrolling.
                            cache: true
                            // iPhone photos are 12MP+; decoding at full
                            // resolution for a 300px view wastes memory.
                            sourceSize.width: 640
                            smooth: true
                            mipmap: true
                            // Honour the EXIF orientation tag. Phone photos
                            // are very often stored rotated with the true
                            // orientation only in metadata, so without this
                            // they display sideways.
                            autoTransform: true
                            // Fade in once decoded. Historical scroll-in
                            // uses the cache and often lands Ready on the
                            // first frame — Behavior then no-ops from 1→1.
                            opacity: status === Image.Ready ? 1 : 0
                            Behavior on opacity {
                                NumberAnimation {
                                    duration: Theme.animBase
                                    easing.type: Easing.OutCubic
                                }
                            }
                            // Search/quote highlight pulse (same rim as text
                            // bubbles). Stickers and photos share it.
                            scale: msgDelegate.highlight ? msgDelegate.rimScale
                                                         : 1.0
                            transformOrigin: Item.Center

                            // Photos get rounded corners; stickers stay bare
                            // and keep their transparency.
                            layer.enabled: isImage && showMedia
                            layer.effect: OpacityMask { maskSource: imgMask }

                            TapHandler {
                                enabled: image !== ""
                                onTapped: Qt.openUrlExternally(image)
                            }
                        }

                        Rectangle {
                            id: imgMask
                            width: mediaItem.width
                            height: mediaItem.height
                            radius: 16
                            color: "white"
                            visible: false
                            layer.enabled: true
                            layer.smooth: true
                        }

                        // ---- text / file chip: normal bubble ------------
                        Rectangle {
                            id: bubble
                            // Successful media draws only the Image above.
                            // Failed/empty media falls through to the chip
                            // so the user still sees a name and can open it.
                            visible: !showMedia

                            anchors {
                                right: outgoing ? parent.right : undefined
                                left: outgoing ? undefined : parent.left
                                rightMargin: msgDelegate.sideMargin
                                leftMargin: 16 + gutter
                            }
                            y: 2
                            // Image/sticker rows carry fileLabel as a decode
                            // fallback; treat them as chips when media failed.
                            readonly property bool isFile:
                                fileLabel !== "" && (!isMedia || !showMedia)
                            readonly property int padH: isJumbo ? 4 : 28
                            readonly property int padV: isJumbo ? 2 : 16
                            width: isFile
                                   ? Math.min(240, msgList.width * 0.6)
                                   : Math.min(msgText.implicitWidth + padH,
                                              msgList.width * 0.68)
                            height: isFile ? 40 : msgText.implicitHeight + padV
                            radius: Theme.radiusBubble
                            // Body and tail are one filled path (see below),
                            // so this Rectangle is only geometry now. Painting
                            // them separately showed a seam: the bubbles are
                            // translucent over KWin's blur, so any overlap
                            // doubled the alpha and read as a stuck-on piece.
                            color: "transparent"
                            // Soft pulse when this is the search/quote jump
                            // target — settles back so the accent rim stays
                            // as the steady cue.
                            scale: msgDelegate.highlight ? msgDelegate.rimScale
                                                         : 1.0
                            transformOrigin: Item.Center

                            Shape {
                                anchors.fill: parent
                                visible: !isJumbo
                                // Curve renderer antialiases the tail's
                                // curves without the MSAA layer a Canvas or
                                // layer.samples would have cost per bubble.
                                preferredRendererType: Shape.CurveRenderer

                                ShapePath {
                                    fillColor: outgoing ? "transparent"
                                                        : Theme.bubbleIn
                                    fillGradient: outgoing ? outGradient : null
                                    // Search / reply-quote jump lights the
                                    // rim. Stroke width and path inset stay
                                    // paired (see bubblePath) so a 2px accent
                                    // does not clip into pixel crumbs.
                                    strokeColor: msgDelegate.highlight
                                                 ? Theme.accent
                                                 : Theme.bubbleRim
                                    strokeWidth: msgDelegate.highlight ? 2 : 1
                                    // Soften the accent rim on/off so a search
                                    // jump does not flash a hard 1px→2px cut.
                                    Behavior on strokeColor {
                                        ColorAnimation { duration: Theme.animBase }
                                    }
                                    joinStyle: ShapePath.RoundJoin
                                    capStyle: ShapePath.RoundCap
                                    PathSvg {
                                        path: msgDelegate.bubblePath(
                                            bubble.width, bubble.height,
                                            Theme.radiusBubble,
                                            tail && !isMedia, outgoing,
                                            msgDelegate.highlight ? 2 : 1)
                                    }
                                }

                                Shapes.LinearGradient {
                                    id: outGradient
                                    x1: 0; y1: 0
                                    x2: 0; y2: bubble.height
                                    GradientStop { position: 0.0; color: Theme.bubbleOutTop }
                                    GradientStop { position: 1.0; color: Theme.bubbleOutBot }
                                }
                            }

                            Row {
                                visible: bubble.isFile
                                anchors.centerIn: parent
                                spacing: 7
                                // Slight lift on hover so a tappable file
                                // chip reads as a control, not flat chrome.
                                opacity: fileChipHover.hovered ? 1.0 : 0.92
                                scale: fileChipHover.hovered ? 1.02 : 1.0
                                Behavior on opacity {
                                    NumberAnimation { duration: Theme.animFast }
                                }
                                Behavior on scale {
                                    NumberAnimation {
                                        duration: Theme.animFast
                                        easing.type: Easing.OutCubic
                                    }
                                }
                                HoverHandler {
                                    id: fileChipHover
                                    cursorShape: Qt.PointingHandCursor
                                }
                                Icon {
                                    name: "paperclip"
                                    size: 14
                                    color: outgoing ? "white" : Theme.text
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Text {
                                    text: fileLabel
                                    color: outgoing ? "white" : Theme.text
                                    font.pixelSize: 12
                                    elide: Text.ElideMiddle
                                    width: 170
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            // TextEdit rather than Text so the message can be
                            // selected and copied with the mouse — Text has no
                            // selection at all. Read-only and with no frame,
                            // so it still looks and measures like a label:
                            // implicitWidth/implicitHeight drive the bubble
                            // geometry above exactly as before.
                            TextEdit {
                                id: msgText
                                readOnly: true
                                selectByMouse: true
                                // The context menu is modal, so opening it
                                // takes focus and would otherwise clear the
                                // selection before Copy could read it.
                                persistentSelection: true
                                // Selecting needs focus, but the caret would
                                // then blink inside a message bubble.
                                cursorVisible: false
                                // The default highlight is the accent blue,
                                // which on an outgoing bubble is the bubble's
                                // own colour — selected text there would be
                                // invisible.
                                selectionColor: outgoing
                                                ? Qt.rgba(1, 1, 1, 0.35)
                                                : Theme.accent
                                selectedTextColor: "white"
                                visible: !bubble.isFile
                                anchors {
                                    left: parent.left; right: parent.right
                                    verticalCenter: parent.verticalCenter
                                    leftMargin: bubble.padH / 2
                                    rightMargin: bubble.padH / 2
                                }
                                horizontalAlignment: isJumbo && outgoing
                                                     ? Text.AlignRight
                                                     : Text.AlignLeft
                                // Rich text only when there's emoji to
                                // resize — it's a heavier layout path, and
                                // most messages don't need it.
                                textFormat: richBody === "" ? Text.PlainText
                                                            : Text.RichText
                                text: mediaOnly
                                      ? "📎 Attachment — not sent over Bluetooth"
                                      : (richBody === "" ? body : richBody)
                                font.italic: mediaOnly
                                wrapMode: Text.Wrap
                                font.pixelSize: Math.round(13 * Theme.fontScale)
                                color: mediaOnly
                                       ? (outgoing ? Qt.rgba(1, 1, 1, 0.75)
                                                   : Theme.textDim)
                                       : (outgoing ? "white" : Theme.bubbleInText)
                            }

                            TapHandler {
                                enabled: bubble.isFile && image !== ""
                                onTapped: Qt.openUrlExternally(image)
                            }
                        }

                        // Sender avatar — drawn once per run, beside the
                        // last message in it.
                        Avatar {
                            visible: senderAvatar !== "" || senderInitials !== ""
                            size: 30
                            source: senderAvatar
                            initials: senderInitials || "?"
                            x: 12
                            anchors {
                                bottom: showMedia ? mediaItem.bottom : bubble.bottom
                            }
                        }

                        // Anchor for the row height + the tapback badge.
                        Item {
                            id: content
                            width: 1
                            height: showMedia ? mediaItem.height : bubble.height
                        }

                        // The Show Times gutter. Deliberately outside the
                        // slide the bubbles make: it stays pinned to the
                        // window edge so both sides of the conversation read
                        // down one column, which is the whole point of it.
                        Text {
                            id: timeLabel
                            text: msgDelegate.timeStamp
                            visible: opacity > 0
                            opacity: msgArea.showTimes ? 1 : 0
                            Behavior on opacity {
                                NumberAnimation {
                                    duration: Theme.animBase
                                    easing.type: Easing.OutCubic
                                }
                            }
                            color: Theme.textDim
                            font.pixelSize: 11
                            horizontalAlignment: Text.AlignRight
                            // Deliberately unsized: a fixed width narrower
                            // than "12:24 PM" does not elide it, it paints
                            // straight through the edge it was meant to
                            // respect. The gutter is sized for the text
                            // instead, not the other way round.
                            anchors {
                                right: parent.right
                                rightMargin: 18
                                verticalCenter: msgDelegate.showMedia
                                                ? mediaItem.verticalCenter
                                                : bubble.verticalCenter
                            }
                        }

                        // Tapbacks, overlapping the corner of the bubble
                        // nearest the centre of the view, as in Messages.app.
                        //
                        // Bare emoji, no disc behind them: they used to be
                        // six hand-drawn SVGs, which meant an iOS 18 emoji
                        // tapback had to borrow an empty bubble and get
                        // painted into a hole measured off the artwork. Every
                        // reaction is an emoji now, so none of them is a
                        // special case.
                        //
                        // Live arrivals pop in; historical badges drawn when
                        // a delegate is first built stay still — `_live` is
                        // flipped only after children complete, so scrolling
                        // old reactions into view does not re-animate them.
                        Row {
                            id: reactionBadge
                            readonly property int count: msgDelegate.reactions.length
                            // Stay painted while fading out so a removal is
                            // not a hard cut.
                            visible: count > 0 || opacity > 0.01
                            opacity: count > 0 ? 1 : 0
                            scale: count > 0 ? 1 : 0.6
                            transformOrigin: Item.Center
                            property bool _live: false
                            Component.onCompleted: {
                                // After children (including the Repeater's
                                // Text items) have completed, so their
                                // onCompleted sees `_live` still false.
                                Qt.callLater(function () {
                                    reactionBadge._live = true
                                })
                            }
                            Behavior on opacity {
                                enabled: reactionBadge._live
                                NumberAnimation { duration: Theme.animFast }
                            }
                            Behavior on scale {
                                enabled: reactionBadge._live
                                NumberAnimation {
                                    duration: Theme.animBase
                                    easing.type: Easing.OutCubic
                                }
                            }
                            // Negative: a message can hold a tapback from
                            // everyone in the thread, and a plain row of them
                            // walks off across the conversation. Overlapped,
                            // they read as one cluster — and because later
                            // siblings paint over earlier ones, the newest
                            // reaction lands on top without touching z.
                            //
                            // About a third of each emoji is covered: enough
                            // to read as a stack without hiding what any of
                            // them are.
                            spacing: -Math.round(reactionBadge.glyph * 0.34)
                            // Roughly the width one emoji occupies at this
                            // size — the margins below are fractions of it, so
                            // the cluster keeps sitting on the bubble's corner
                            // if the size ever changes.
                            readonly property int glyph: 20
                            anchors {
                                // Straddling the bubble's top edge, not
                                // floating above it: at a whole glyph of
                                // negative margin a single reaction cleared
                                // the corner entirely and looked unattached
                                // to the message it belonged to. Two-thirds
                                // of it now sits over the bubble.
                                verticalCenter: showMedia ? mediaItem.top : bubble.top
                                verticalCenterOffset: Math.round(glyph * 0.15)
                                left: outgoing ? (showMedia ? mediaItem.left : bubble.left)
                                               : undefined
                                right: outgoing ? undefined
                                                : (showMedia ? mediaItem.right : bubble.right)
                                leftMargin: -Math.round(glyph * 0.35)
                                rightMargin: -Math.round(glyph * 0.35)
                            }

                            Repeater {
                                model: msgDelegate.reactions
                                Text {
                                    id: reactionGlyph
                                    required property string modelData
                                    required property int index
                                    text: modelData
                                    font.pixelSize: 22
                                    // New glyphs (live reaction while the
                                    // bubble is on screen) pop in; glyphs
                                    // built with the delegate do not.
                                    opacity: 1
                                    scale: 1
                                    transformOrigin: Item.Center
                                    Component.onCompleted: {
                                        if (!reactionBadge._live)
                                            return
                                        opacity = 0
                                        scale = 0.6
                                        reactionAppear.start()
                                    }
                                    ParallelAnimation {
                                        id: reactionAppear
                                        NumberAnimation {
                                            target: reactionGlyph
                                            property: "opacity"; to: 1
                                            duration: Theme.animBase
                                        }
                                        NumberAnimation {
                                            target: reactionGlyph
                                            property: "scale"; to: 1
                                            duration: Theme.animBase
                                            easing.type: Easing.OutBack
                                            easing.overshoot: 1.4
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Delivery state / Edited marker under the bubble.
                    // Outgoing (blue): right-aligned with the bubble wall.
                    // Incoming (grey): left-aligned with the grey bubble
                    // (`16 + gutter`), so a solo "Edited" sits under its
                    // own side rather than floating on the far right.
                    Item {
                        id: captionLine
                        // "Edited" earns the line on its own, even with no
                        // receipt yet — an edit you made is worth confirming.
                        readonly property bool hasEdits:
                            msgDelegate.edits.length > 0
                        readonly property bool shown:
                            msgDelegate.deliveryState !== "" || hasEdits
                        // Receipts only exist on outgoing; use the bubble
                        // side so an edited grey bubble mirrors blue.
                        readonly property bool alignRight: msgDelegate.outgoing

                        width: msgList.width
                        // Animated, so the thread opens the space for a
                        // receipt over several frames instead of jumping by
                        // 14px in one. The view welds itself to the bottom
                        // while this runs, so what you see is the whole
                        // conversation easing up by the height of the line.
                        height: shown ? 14 : 0
                        Behavior on height {
                            enabled: !msgDelegate.freshlyAdded
                            NumberAnimation {
                                duration: Theme.animArrive
                                easing.type: Easing.OutCubic
                            }
                        }
                        // Not `shown`: it has to stay rendered while the
                        // height animates back to zero.
                        visible: height > 0

                        Row {
                            // Fades up into place when a receipt lands, the
                            // same motion a new bubble makes. The band itself
                            // takes its 14px immediately — animating the
                            // height would drip dozens of tiny content-height
                            // changes into the view's bottom-follow, which
                            // expects one. So the layout moves once, smoothly,
                            // under the eased scroll, and the text rises
                            // within the space that opened for it.
                            //
                            // Bindings, not a Transition: on a delegate built
                            // with the caption already there, both evaluate to
                            // their resting values and nothing animates —
                            // which is what scrolling old messages into view
                            // should do.
                            opacity: captionLine.shown ? 1 : 0
                            Behavior on opacity {
                                enabled: !msgDelegate.freshlyAdded
                                NumberAnimation {
                                    duration: Theme.animArrive
                                    easing.type: Easing.OutCubic
                                }
                            }
                            transform: Translate {
                                y: captionLine.shown ? 0 : 6
                                Behavior on y {
                                    enabled: !msgDelegate.freshlyAdded
                                    NumberAnimation {
                                        duration: Theme.animArrive
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }

                            anchors.right: captionLine.alignRight
                                           ? parent.right : undefined
                            anchors.left: captionLine.alignRight
                                          ? undefined : parent.left
                            // Flush with the bubble wall on the matching
                            // side — same margins the bubble itself uses.
                            anchors.rightMargin: msgDelegate.sideMargin
                            anchors.leftMargin: 16 + msgDelegate.gutter
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 3

                            Text {
                                id: deliveryLabel
                                visible: msgDelegate.deliveryState !== ""
                                text: msgDelegate.deliveryState === "read"
                                      ? "Read" : "Delivered"
                                // Read is slightly brighter so the upgrade
                                // from Delivered is visible without a flash.
                                color: msgDelegate.deliveryState === "read"
                                       ? Theme.text : Theme.textDim
                                font.pixelSize: 10
                                font.bold: msgDelegate.deliveryState === "read"
                                Behavior on color {
                                    ColorAnimation { duration: Theme.animBase }
                                }
                                // Soft dip when Delivered → Read (or the
                                // reverse) so the word change is felt, not
                                // only swapped. Skip the first paint so
                                // scrolling history in does not pulse.
                                property bool _live: false
                                property string _state: msgDelegate.deliveryState
                                Component.onCompleted: {
                                    Qt.callLater(function () {
                                        deliveryLabel._live = true
                                    })
                                }
                                on_StateChanged: {
                                    if (!_live || _state === "")
                                        return
                                    deliveryPulse.restart()
                                }
                                SequentialAnimation {
                                    id: deliveryPulse
                                    NumberAnimation {
                                        target: deliveryLabel; property: "opacity"
                                        to: 0.35; duration: Theme.animMicro
                                    }
                                    NumberAnimation {
                                        target: deliveryLabel; property: "opacity"
                                        to: 1.0; duration: Theme.animBase
                                        easing.type: Easing.OutCubic
                                    }
                                }
                            }

                            Text {
                                visible: msgDelegate.deliveryStamp !== ""
                                text: msgDelegate.deliveryStamp
                                color: Theme.textDim
                                font.pixelSize: 10
                            }

                            Text {
                                visible: captionLine.hasEdits
                                         && msgDelegate.deliveryState !== ""
                                text: "·"
                                color: Theme.textDim
                                font.pixelSize: 10
                            }

                            Text {
                                id: editedLink
                                visible: captionLine.hasEdits
                                text: "Edited"
                                // Blue and underlined on hover: the only
                                // part of a caption that does anything, so
                                // it has to look unlike the rest of it.
                                color: Theme.accent
                                font.pixelSize: 10
                                font.underline: editedHover.hovered

                                HoverHandler {
                                    id: editedHover
                                    cursorShape: Qt.PointingHandCursor
                                }
                                TapHandler {
                                    onTapped: msgDelegate.showEdits =
                                              !msgDelegate.showEdits
                                }
                            }
                        }
                    }
                }
            }


                // Contact photo + name, centred over the conversation.
                // openOpacity covers empty↔thread; swapOpacity crossfades
                // between two open threads. Display fields update only at
                // the bottom of the dip so the new name never flashes in
                // before the old one has faded out.
                Column {
                    id: peerHeader
                    z: 2
                    property real openOpacity: msgArea.hasThread ? 1 : 0
                    property real swapOpacity: 1
                    property string displayName: ""
                    property string displayAvatar: ""
                    property string displayInitials: ""
                    opacity: openOpacity * swapOpacity
                    visible: opacity > 0.01
                    Behavior on openOpacity {
                        NumberAnimation {
                            duration: Theme.animFast
                            easing.type: Easing.OutCubic
                        }
                    }
                    transform: Translate {
                        y: peerHeader.openOpacity > 0.5 ? 0 : 6
                        Behavior on y {
                            NumberAnimation {
                                duration: Theme.animFast
                                easing.type: Easing.OutCubic
                            }
                        }
                    }
                    function syncDisplay() {
                        displayName = threadStore.peerName
                        displayAvatar = threadStore.peerAvatar
                        displayInitials = threadStore.peerInitials
                    }
                    Component.onCompleted: syncDisplay()
                    // Only crossfade when already open — first open rides
                    // openOpacity alone and syncs the label immediately.
                    property string _key: threadStore.currentKey
                    on_KeyChanged: {
                        if (openOpacity > 0.9 && _key !== "")
                            headerSwap.restart()
                        else {
                            headerSwap.stop()
                            swapOpacity = 1
                            syncDisplay()
                        }
                    }
                    SequentialAnimation {
                        id: headerSwap
                        NumberAnimation {
                            target: peerHeader; property: "swapOpacity"
                            to: 0; duration: Theme.animMicro
                        }
                        ScriptAction { script: peerHeader.syncDisplay() }
                        NumberAnimation {
                            target: peerHeader; property: "swapOpacity"
                            to: 1; duration: Theme.animFast
                            easing.type: Easing.OutCubic
                        }
                    }
                    anchors {
                        top: parent.top
                        topMargin: 8
                        horizontalCenter: parent.horizontalCenter
                    }
                    spacing: 4

                    Avatar {
                        anchors.horizontalCenter: parent.horizontalCenter
                        size: 36
                        source: peerHeader.displayAvatar
                        initials: peerHeader.displayInitials
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: peerHeader.displayName
                        color: Theme.text
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                }
            }

            // Context banner: what the next Enter will do, when it isn't
            // simply "send a new message". Without this, reply and edit mode
            // are invisible states and the next keystroke is a surprise.
            // Height opens the strip; content opacity eases so cancel is a
            // fade rather than a hard clip of the label mid-collapse.
            Rectangle {
                id: pendingBanner
                Layout.fillWidth: true
                Layout.leftMargin: 12
                Layout.rightMargin: 12
                Layout.preferredHeight: composer.pendingGuid === "" ? 0 : 28
                visible: Layout.preferredHeight > 0.5 || bannerBody.opacity > 0.01
                radius: 8
                color: Qt.rgba(1, 1, 1, 0.06)
                clip: true

                Behavior on Layout.preferredHeight {
                    NumberAnimation { duration: Theme.animFast }
                }

                Row {
                    id: bannerBody
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 6
                    spacing: 8
                    opacity: composer.pendingGuid !== "" ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation { duration: Theme.animFast }
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: composer.editing ? "Editing" : "Replying to"
                        color: Theme.accent
                        font.pixelSize: 11
                        font.bold: true
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - 120
                        text: composer.pendingBody
                        color: Theme.textDim
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }

                // Escape also cancels; this is for the mouse.
                Icon {
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    anchors.verticalCenter: parent.verticalCenter
                    name: "x"
                    size: 12
                    color: Theme.textDim
                    opacity: bannerBody.opacity
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -6
                        enabled: composer.pendingGuid !== ""
                        cursorShape: Qt.PointingHandCursor
                        onClicked: composer.cancelPending()
                    }
                }
            }

            // Pill compose field, no send button — Enter sends, Shift+Enter
            // starts a new line.
            Rectangle {
                id: composer
                Layout.fillWidth: true
                // Collapse margins with the field so a closed thread leaves
                // no residual strip under the message area.
                Layout.margins: msgArea.hasThread ? 12 : 0
                // Grows with the text and then stops, so a long message
                // scrolls inside the pill rather than eating the
                // conversation. 38 keeps the single-line pill exactly as it
                // was. Height 0 when no thread so empty-state and open
                // share one eased shell rather than a hard show/hide.
                readonly property int maxHeight: 140
                // Same measurement the field uses, so the pill and its
                // contents can never disagree about how tall one line is.
                Layout.preferredHeight: msgArea.hasThread
                    ? Math.min(maxHeight, Math.max(38, composeFlick.height + 12))
                    : 0
                opacity: msgArea.hasThread ? 1 : 0
                // Stay in the tree while fading so the rise is visible.
                visible: Layout.preferredHeight > 0.5 || opacity > 0.01
                clip: true
                Behavior on Layout.preferredHeight {
                    NumberAnimation {
                        duration: Theme.animBase
                        easing.type: Easing.OutCubic
                    }
                }
                Behavior on opacity {
                    NumberAnimation {
                        duration: Theme.animBase
                        easing.type: Easing.OutCubic
                    }
                }
                transform: Translate {
                    y: msgArea.hasThread ? 0 : 8
                    Behavior on y {
                        NumberAnimation {
                            duration: Theme.animBase
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                // Non-empty while composing a reply or an edit. `editing`
                // distinguishes the two, since both hold a guid.
                property string pendingGuid: ""
                property string pendingBody: ""
                property bool editing: false

                // Both refuse an empty guid rather than quietly dropping into
                // plain-send mode. The menu already hides these, but a stale
                // delegate could still get here, and silently sending an
                // unthreaded message is the worst available failure: it looks
                // like nothing happened until it turns up as a normal text.
                function beginReply(guid, body) {
                    if (guid === "")
                        return
                    pendingGuid = guid
                    pendingBody = body
                    editing = false
                    composeField.forceActiveFocus()
                }

                function beginEdit(guid, body) {
                    if (guid === "")
                        return
                    pendingGuid = guid
                    pendingBody = body
                    editing = true
                    // Edit starts from the existing text — you're amending
                    // it, not retyping it.
                    composeField.text = body
                    composeField.forceActiveFocus()
                }

                function cancelPending() {
                    if (editing)
                        composeField.text = ""
                    pendingGuid = ""
                    pendingBody = ""
                    editing = false
                }

                // Dispatch on whichever mode is active.
                function submit(text) {
                    if (text.trim() === "")
                        return
                    if (pendingGuid !== "" && editing)
                        threadStore.editMessage(pendingGuid, text)
                    else if (pendingGuid !== "")
                        threadStore.replyTo(pendingGuid, text)
                    else
                        threadStore.send(text)
                    pendingGuid = ""
                    pendingBody = ""
                    editing = false
                    // Composer-only cue. The outgoing bubble already has its
                    // own add transition — scaling that too would double up.
                    sendPulse.restart()
                }
                // Brief press-in on send so Enter has tactile feedback even
                // though there is no send button.
                SequentialAnimation {
                    id: sendPulse
                    NumberAnimation {
                        target: composer; property: "scale"
                        to: 0.97
                        duration: Theme.animMicro
                        easing.type: Easing.OutCubic
                    }
                    NumberAnimation {
                        target: composer; property: "scale"
                        to: 1.0
                        duration: Theme.animFast
                        easing.type: Easing.OutCubic
                    }
                }
                // Pill while it's one line; once it grows, cap the corner so
                // it becomes a rounded box rather than a lozenge.
                radius: Math.min(height / 2, 19)
                color: Qt.rgba(1, 1, 1, 0.07)
                border.width: composeField.activeFocus ? 1.5 : 1
                border.color: composeField.activeFocus
                              ? Theme.accent : Theme.separator
                transformOrigin: Item.Center

                Behavior on border.color { ColorAnimation { duration: Theme.animFast } }

                // The TextArea lives inside a Flickable via the attached
                // `TextArea.flickable`, which is what keeps the caret in view
                // once the message outgrows `maxHeight`. A bare TextArea does
                // not scroll, so past the cap you would be typing into text
                // you cannot see.
                FontMetrics {
                    id: composeMetrics
                    font: composeField.font
                }

                // The placeholder. A sibling of the field rather than a child
                // of it, so it takes no part in the text layout at all — it
                // simply sits where the first line of text will sit, computed
                // from the same padding the field uses. Fades when the first
                // character lands so typing does not hard-cut the prompt.
                Text {
                    id: composePlaceholder
                    opacity: composeField.length === 0 ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity {
                        NumberAnimation { duration: Theme.animFast }
                    }
                    text: "Message"
                    color: Theme.textDim
                    font: composeField.font
                    elide: Text.ElideRight
                    x: composeFlick.x + composeField.leftPadding
                    y: composeFlick.y + composeField.topPadding
                    width: composeFlick.width - composeField.leftPadding
                           - composeField.rightPadding
                }

                Flickable {
                    id: composeFlick
                    anchors.left: parent.left
                    anchors.right: parent.right
                    // Inset from the pill's rounded edge, not from its bounding
                    // box — at radius 19 the border curves in, so a small
                    // margin puts the caret visually tight against the stroke.
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    // Centred and sized to the text, not filling the pill.
                    // Filling it pinned the first line to the top, so a
                    // one-line message — and the placeholder — sat high in
                    // the pill instead of on its centre line.
                    anchors.verticalCenter: parent.verticalCenter
                    // Measured from the font and the line count, not from
                    // `composeField.implicitHeight`. The implicit height is a
                    // layout result, and on the very first frame — before the
                    // TextArea has been given its width and laid its
                    // placeholder out — it is wrong, so the field opened
                    // off-centre and only corrected once a keystroke forced a
                    // relayout. Line count and font metrics are known before
                    // any of that happens.
                    height: Math.min(composer.maxHeight - 12,
                                     Math.ceil(Math.max(
                                         composeField.contentHeight,
                                         composeMetrics.height))
                                     + composeField.topPadding
                                     + composeField.bottomPadding)
                    // Rounded up, because the two measurements disagree by
                    // half a pixel: font metrics said 15.5 where the laid-out
                    // line is 16. That left the view half a pixel shorter
                    // than its content, so it could scroll — and a caret on a
                    // half-pixel boundary is exactly the smeared, misplaced
                    // caret this was.
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    // While the field is empty the attached TextArea has not
                    // published a content height yet, and the Flickable's own
                    // is -1. Its bounds are then meaningless and contentY
                    // drifted to -6, drawing the caret six pixels below the
                    // line the text will actually occupy — the offset that
                    // corrected itself as soon as a keystroke or a second
                    // click forced the layout. Stating it keeps the bounds
                    // sane from the first frame.
                    contentHeight: Math.max(composeField.implicitHeight, height)

                    TextArea.flickable: TextArea {
                    id: composeField
                    // Deliberately not focused on startup. Taking focus put a
                    // caret in the field before its layout had settled, so the
                    // caret sat off its line until a click forced a relayout —
                    // the same first-frame problem the placeholder had, and it
                    // cannot be positioned by hand the way the placeholder is.
                    // Clicking the field, or Message → Compose, still focuses
                    // it (see focusComposer).
                    background: null
                    color: Theme.text
                    // Drawn by hand below instead. The built-in placeholder is
                    // a separate Text laid out against the control's height,
                    // and inside a Flickable that height is not settled on the
                    // first frame — so the prompt rendered off its line and
                    // only corrected when focus forced a relayout. Ours is
                    // positioned from the padding, which is a constant.
                    placeholderText: ""
                    font.pixelSize: 13
                    // Wrap rather than scroll sideways; Shift+Enter adds the
                    // explicit breaks.
                    wrapMode: TextArea.Wrap
                    topPadding: 6
                    bottomPadding: 6
                    leftPadding: 0
                    rightPadding: 0

                    // ---- :emoji autocomplete ---------------------------
                    property int tokenStart: -1
                    property var suggestions: []
                    property int highlighted: 0
                    readonly property bool popupOpen:
                        tokenStart >= 0 && suggestions.length > 0

                    function refreshSuggestions() {
                        var tok = emoji.tokenAt(text, cursorPosition)
                        if (tok.start < 0) {
                            tokenStart = -1
                            suggestions = []
                            return
                        }
                        var hits = emoji.search(tok.query)
                        tokenStart = hits.length > 0 ? tok.start : -1
                        suggestions = hits
                        highlighted = 0
                    }

                    function applySuggestion(index) {
                        if (!popupOpen)
                            return
                        var pick = suggestions[Math.max(0, Math.min(
                            index, suggestions.length - 1))]
                        var before = text.slice(0, tokenStart)
                        var after = text.slice(cursorPosition)
                        var insert = pick.emoji + " "
                        text = before + insert + after
                        cursorPosition = before.length + insert.length
                        tokenStart = -1
                        suggestions = []
                    }

                    onTextChanged: {
                        refreshSuggestions()
                        // Tell the other side we're composing, the way
                        // iMessage does. Sent on the first keystroke of a
                        // burst rather than every one — the indicator has
                        // its own timeout on their end, so repeating adds
                        // nothing.
                        if (text !== "") {
                            if (!typingStop.running)
                                threadStore.setTyping(true)
                            typingStop.restart()
                        }
                    }
                    onCursorPositionChanged: refreshSuggestions()

                    Timer {
                        id: typingStop
                        interval: 5000
                        onTriggered: threadStore.setTyping(false)
                    }

                    Keys.onPressed: (event) => {
                        var isReturn = event.key === Qt.Key_Return
                                       || event.key === Qt.Key_Enter
                        if (!popupOpen) {
                            // Back out of reply/edit mode. Only reachable
                            // once the emoji popup has had its turn, so
                            // Escape unwinds one layer at a time.
                            if (event.key === Qt.Key_Escape
                                    && composer.pendingGuid !== "") {
                                composer.cancelPending()
                                event.accepted = true
                                return
                            }
                            // Shift+Enter is a line break: left unaccepted so
                            // TextArea inserts it itself. Plain Enter sends —
                            // handled here rather than through `onAccepted`,
                            // which a TextArea does not have.
                            if (isReturn && !(event.modifiers & Qt.ShiftModifier)) {
                                composer.submit(text)
                                text = ""
                                typingStop.stop()
                                threadStore.setTyping(false)
                                event.accepted = true
                            }
                            return
                        }
                        // Tab takes the highlighted entry (the first, until
                        // arrows move it) — the Discord/Slack muscle memory.
                        if (event.key === Qt.Key_Tab) {
                            applySuggestion(highlighted)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Down) {
                            highlighted = Math.min(highlighted + 1,
                                                   suggestions.length - 1)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Up) {
                            highlighted = Math.max(highlighted - 1, 0)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Escape) {
                            tokenStart = -1
                            suggestions = []
                            // Emoji popup first, reply/edit mode second:
                            // Escape should back out one layer at a time.
                            event.accepted = true
                        } else if (isReturn) {
                            // Complete rather than sending a half-typed
                            // ":smi" — matches how Discord behaves.
                            applySuggestion(highlighted)
                            event.accepted = true
                        }
                    }

                    }
                }

                // ---- suggestion popup, floating above the composer ----
                Popup {
                    id: emojiPopup
                    visible: composeField.popupOpen
                    y: -height - 8
                    x: 6
                    width: 290
                    padding: 5
                    closePolicy: Popup.NoAutoClose
                    // Grows up from the composer field edge.
                    transformOrigin: Item.Bottom

                    background: Rectangle {
                        color: Theme.dark ? Qt.rgba(0.16, 0.16, 0.18, 0.97)
                                          : Qt.rgba(1, 1, 1, 0.97)
                        radius: 12
                        border.width: 1
                        border.color: Theme.separator
                    }

                    enter: Transition {
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; from: 0; to: 1
                                duration: Theme.animFast
                            }
                            NumberAnimation {
                                property: "scale"
                                from: Theme.popScale; to: 1
                                duration: Theme.animFast
                                easing.type: Easing.OutCubic
                            }
                        }
                    }
                    exit: Transition {
                        ParallelAnimation {
                            NumberAnimation {
                                property: "opacity"; to: 0
                                duration: Theme.animMicro
                            }
                            NumberAnimation {
                                property: "scale"; to: Theme.popScale
                                duration: Theme.animMicro
                            }
                        }
                    }

                    contentItem: Column {
                        spacing: 1
                        Repeater {
                            model: composeField.suggestions
                            delegate: Rectangle {
                                required property int index
                                required property var modelData
                                width: emojiPopup.width - 10
                                height: 28
                                radius: 7
                                color: index === composeField.highlighted
                                       ? Theme.accent
                                       : (hov.hovered ? Qt.rgba(1, 1, 1, 0.08)
                                                      : "transparent")
                                Behavior on color {
                                    ColorAnimation { duration: Theme.animFast }
                                }

                                HoverHandler {
                                    id: hov
                                    // Hovering moves the selection, so Tab
                                    // and a click always agree.
                                    onHoveredChanged: if (hovered)
                                        composeField.highlighted = index
                                }

                                Row {
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.left: parent.left
                                    anchors.leftMargin: 9
                                    spacing: 9
                                    Text {
                                        text: modelData.emoji
                                        font.pixelSize: 15
                                        anchors.verticalCenter: parent.verticalCenter
                                        scale: (index === composeField.highlighted
                                                || hov.hovered)
                                               ? Theme.hoverScale : 1.0
                                        Behavior on scale {
                                            NumberAnimation {
                                                duration: Theme.animFast
                                                easing.type: Easing.OutCubic
                                            }
                                        }
                                    }
                                    Text {
                                        text: ":" + modelData.code
                                        color: index === composeField.highlighted
                                               ? "white" : Theme.textDim
                                        font.pixelSize: 12
                                        anchors.verticalCenter: parent.verticalCenter
                                        Behavior on color {
                                            ColorAnimation { duration: Theme.animFast }
                                        }
                                    }
                                }

                                TapHandler {
                                    onTapped: {
                                        composeField.applySuggestion(index)
                                        composeField.forceActiveFocus()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
