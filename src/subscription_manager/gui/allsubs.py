#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import datetime
import gettext
import gobject
import logging

import gtk

from subscription_manager import async
from subscription_manager.gui.contract_selection import ContractSelectionWindow
from subscription_manager.gui.filter import FilterOptionsWindow, Filters
from subscription_manager.gui import progress
from subscription_manager.gui.storage import MappedTreeStore
from subscription_manager.gui.utils import apply_highlight, show_error_window, get_cell_background_color, handle_gui_exception, set_background_model_index
from subscription_manager.gui import widgets
from subscription_manager.injection import IDENTITY, require
from subscription_manager.jsonwrapper import PoolWrapper
from subscription_manager import managerlib
from subscription_manager.managerlib import allows_multi_entitlement, valid_quantity
from subscription_manager import plugins

_ = gettext.gettext

log = logging.getLogger('rhsm-app.' + __name__)


class AllSubscriptionsTab(widgets.SubscriptionManagerTab):
    widget_names = widgets.SubscriptionManagerTab.widget_names + \
                       ['details_box', 'date_picker_hbox',
                        'month_entry', 'day_entry', 'year_entry',
                        'active_on_checkbutton', 'subscribe_button',
                        'edit_quantity_label', 'no_subs_label',
                        'filter_options_button', 'applied_filters_label']

    def __init__(self, backend, facts, parent_win):

        super(AllSubscriptionsTab, self).__init__('allsubs.glade')

        self.parent_win = parent_win
        self.backend = backend
        self.identity = require(IDENTITY)
        self.facts = facts

        self.pool_stash = managerlib.PoolStash(self.backend,
                                               self.facts)
        self.plugin_manager = plugins.get_plugin_manager()

        today = datetime.date.today()
        self.date_picker = widgets.DatePicker(today)
        self.date_picker_hbox.add(self.date_picker)

        # Custom build of the subscription column.
        title_text_renderer = gtk.CellRendererText()
        title_text_renderer.set_property('xalign', 0.0)
        subscription_column = gtk.TreeViewColumn(_('Subscription'),
                                        title_text_renderer,
                                        markup=self.store['product_name_formatted'])
        subscription_column.set_expand(True)
        self.top_view.append_column(subscription_column)
        cols = []
        cols.append((subscription_column, 'text', 'product_name_formatted'))

        machine_type_col = widgets.MachineTypeColumn(self.store['virt_only'])
        self.top_view.append_column(machine_type_col)
        cols.append((machine_type_col, 'text', 'virt_only'))

        column = self.add_text_column(_('Available'), 'available')
        cols.append((column, 'text', 'available'))

        # Set up the quantity column.
        quantity_column = widgets.QuantitySelectionColumn(_("Quantity"),
                                                          self.store,
                                                          self.store['quantity_to_consume'],
                                                          self.store['multi-entitlement'],
                                                          self.store['quantity_available'],
                                                          self.store['quantity_increment'])
        self.top_view.append_column(quantity_column)

        self.set_sorts(cols)

        self.edit_quantity_label.set_label(quantity_column.get_column_legend_text())

        # Ensure all cells are colored according the the store.
        set_background_model_index(self.top_view, self.store['background'])

        self.top_view.connect("row_activated",
                              widgets.expand_collapse_on_row_activated_callback)

        # This option should be selected by default:
        self.sub_details = widgets.SubDetailsWidget(backend.product_dir)
        self.details_box.add(self.sub_details.get_widget())

        self.contract_selection = None

        self.filters = Filters(show_compatible=True, show_no_overlapping=True)
        self.filter_dialog = FilterOptionsWindow(self.filters, self)

        self.update_applied_filters_label()
        self.glade.signal_autoconnect({
            "on_search_button_clicked": self.search_button_clicked,
            "on_subscribe_button_clicked": self.subscribe_button_clicked,
            "on_filter_options_button_clicked": self.filter_options_button_clicked,
        })

        # Nothing displayed initially:
        self.clear_pools()

    # Override so that we can use a tree store.
    def get_store(self):
        return MappedTreeStore(self.get_type_map())

    def get_type_map(self):
        return {
            'virt_only': gobject.TYPE_PYOBJECT,
            'product_name': str,
            'available': str,
            'product_id': str,
            'pool_id': str,
            'merged_pools': gobject.TYPE_PYOBJECT,
            'product_name_formatted': str,
            'quantity_to_consume': int,
            'background': str,
            'support_type': str,
            'support_level': str,

            # TODO:  This is not needed here.
            'align': float,
            'multi-entitlement': bool,
            'quantity_available': int,
            'quantity_increment': int,
        }

    def get_filter_text(self):
        """
        Returns the text to filter subscriptions based on. Will return None
        if the text box is empty.
        """
        contains_text = self.filters.contains_text
        if not contains_text:
            contains_text = None

        return contains_text

    def clear_pools(self):
        """
        Clear pools list.
        """
        self.store.clear()
        self.display_message(_("Press Update to search for subscriptions."))

    def display_message(self, message):
        """
        Show a message in situations where we have no subscriptions to show.
        """
        self.top_view.hide()
        self.no_subs_label.set_markup("<b><big>%s</big></b>" % message)
        self.no_subs_label.show()

    def display_pools(self):
        """
        Re-display the list of pools last queried, based on current filter options.
        """
        selection = self.top_view.get_selection()
        selected_pool_id = None
        itr = selection.get_selected()[1]
        if itr:
            selected_pool_id = self.store.get_value(itr, self.store['pool_id'])

        self.store.clear()

        # It may seem backwards that incompatible = self.filters.show_compatible
        # etc., but think of it like "if show_compatible is true, then
        # filter out all the incompatible products."
        merged_pools = self.pool_stash.merge_pools(
                incompatible=self.filters.show_compatible,
                overlapping=self.filters.show_no_overlapping,
                uninstalled=self.filters.show_installed,
                subscribed=True,
                text=self.get_filter_text())

        if self.pool_stash.all_pools_size() == 0:
            self.sub_details.clear()
            self.display_message(_("No subscriptions are available on %s.") %
                                   self.date_picker.date.strftime("%Y-%m-%d"))
            return

        if len(merged_pools) == 0:
            self.sub_details.clear()
            self.display_message(_("No subscriptions match current filters."))
            return

        # Hide the no subscriptions label and show the pools list:
        self.top_view.show()
        self.no_subs_label.hide()

        sorter = managerlib.MergedPoolsStackingGroupSorter(merged_pools.values())
        for group_idx, group in enumerate(sorter.groups):
            bg_color = get_cell_background_color(group_idx)
            tree_iter = None
            if group.name and len(group.entitlements) > 1:
                tree_iter = self.store.add_map(tree_iter, self._create_parent_map(group.name, bg_color))

            for entry in group.entitlements:
                quantity_available = 0
                if entry.quantity < 0:
                    available = _('Unlimited')
                    quantity_available = -1
                else:
                    available = entry.quantity - entry.consumed
                    quantity_available = entry.quantity - entry.consumed

                pool = entry.pools[0]
                attrs = self._product_attrs_to_dict(pool['productAttributes'])

                # Display support level and type if the attributes are present:
                support_level = ""
                support_type = ""
                if 'support_level' in attrs:
                    support_level = attrs['support_level']
                if 'support_type' in attrs:
                    support_type = attrs['support_type']

                quantity_increment = 1
                if 'calculatedAttributes' in pool:
                    calculated_attrs = pool['calculatedAttributes']

                    if 'quantity_increment' in calculated_attrs:
                        quantity_increment = int(calculated_attrs['quantity_increment'])

                self.store.add_map(tree_iter, {
                    'virt_only': self._machine_type(entry.pools),
                    'product_name': entry.product_name,
                    'product_name_formatted': apply_highlight(entry.product_name,
                                                              self.get_filter_text()),
                    'quantity_to_consume': self.calculate_default_quantity(pool),
                    'available': available,
                    'product_id': entry.product_id,
                    'pool_id': entry.pools[0]['id'],  # not displayed, just for lookup later
                    'merged_pools': entry,  # likewise not displayed, for subscription
                    'align': 0.5,
                    'multi-entitlement': allows_multi_entitlement(pool),
                    'background': bg_color,
                    'quantity_available': quantity_available,
                    'support_level': support_level,
                    'support_type': support_type,
                    'quantity_increment': quantity_increment,
                })

        # Ensure that all nodes are expanded in the tree view.
        self.top_view.expand_all()

        # set the selection/details back to what they were, if possible
        found = False
        if selected_pool_id:
            itr = self.store.get_iter_first()
            while itr is not None:
                if self.store.get_value(itr,
                        self.store['pool_id']) == selected_pool_id:
                    self.top_view.set_cursor(self.store.get_path(itr))
                    found = True
                    break
                else:
                    itr = self.store.iter_next(itr)
        if not found:
            self.sub_details.clear()

    def _product_attrs_to_dict(self, product_attributes_list):
        """
        Convert the JSON list of product attributes into a dict we can
        work with more easily.
        """
        final_attrs = {}
        for pa in product_attributes_list:
            final_attrs[pa['name']] = pa['value']
        return final_attrs

    # need to determine what type of machine the product is for
    #  based on the pools accumulated.
    # returns true for virtual, false for physical, and
    #  None for both.
    def _machine_type(self, pools):
        virt_only = None
        first = True
        for pool in pools:
            if first:
                virt_only = PoolWrapper(pool).is_virt_only()
                first = False
            else:
                if virt_only != PoolWrapper(pool).is_virt_only():
                    return None
        return virt_only

    def _create_parent_map(self, title, bg_color):
        return {
                    'virt_only': False,
                    'product_name': title,
                    'product_name_formatted': apply_highlight(title, self.get_filter_text()),
                    'quantity_to_consume': 0,
                    'available': "",
                    'product_id': "",
                    'pool_id': "",  # not displayed, just for lookup later
                    'merged_pools': None,  # likewise not displayed, for subscription
                    'align': 0.5,
                    'multi-entitlement': False,
                    'background': bg_color,
                    'quantity_available': 0,
                    'support_level': "",
                    'support_type': "",
                    'quantity_increment': 1,
                }

    def get_label(self):
        return _("All Available Subscriptions")

    def search_button_clicked(self, widget):
        """
        Reload the subscriptions from the server when the Search button
        is clicked.
        """
        if not self.date_picker.date_entry_validate():
            return
        try:
            async_stash = async.AsyncPool(self.pool_stash)
            async_stash.refresh(self.date_picker.date, self._update_display)
            # show pulsating progress bar while we wait for results
            self.pb = progress.Progress(_("Searching"),
                    _("Searching for subscriptions. Please wait."))
            self.timer = gobject.timeout_add(100, self.pb.pulse)
            self.pb.set_parent_window(self.content.get_parent_window().get_user_data())
        except Exception, e:
            handle_gui_exception(e, _("Error fetching subscriptions from server:  %s"),
                    self.parent_win)

    def _update_display(self, data, error):
        if self.pb:
            self.pb.hide()
            gobject.source_remove(self.timer)
            self.timer = 0
            self.pb = None

        if error:
            handle_gui_exception(error, _("Unable to search for subscriptions:  %s"),
                    self.parent_win)
        else:
            self.display_pools()

    def _contract_selected(self, pool, quantity=1):
        if not valid_quantity(quantity):
            show_error_window(_("Quantity must be a positive number."),
                              parent=self.parent_win)
            return

        self._contract_selection_cancelled()
        try:
            self.plugin_manager.run("pre_subscribe", consumer_uuid=self.identity.uuid,
                                    pool_id=pool['id'], quantity=quantity)
            ents = self.backend.cp_provider.get_user_auth_cp().bindByEntitlementPool(self.identity.uuid, pool['id'], quantity)
            self.plugin_manager.run("post_subscribe", consumer_uuid=self.identity.uuid, entitlement_data=ents)
            managerlib.fetch_certificates(self.backend)

        except Exception, e:
            handle_gui_exception(e, _("Error getting subscription: %s"),
                    self.parent_win)

        #Force the search results to refresh with the new info
        self.search_button_clicked(None)

    def _contract_selection_cancelled(self):
        if self.contract_selection:
            self.contract_selection.destroy()
        self.contract_selection = None

    def update_applied_filters_label(self):
        self.applied_filters_label.set_text(_("%s applied") %
                                              self.filters.get_applied_count())

    def filter_options_button_clicked(self, button):
        self.filter_dialog.show()

    def subscribe_button_clicked(self, button):
        model, tree_iter = self.top_view.get_selection().get_selected()
        merged_pools = model.get_value(tree_iter, self.store['merged_pools'])
        quantity_to_consume = model.get_value(tree_iter, self.store['quantity_to_consume'])

        # Decide if we need to show the contract selection dialog or not.
        # If there's just one pool and does not allow multi-entitlement,
        # shortcut right to the callback that the dialog would have run.
        if len(merged_pools.pools) == 1:
            self._contract_selected(merged_pools.pools[0], quantity_to_consume)
            return

        self.contract_selection = ContractSelectionWindow(
                self._contract_selected, self._contract_selection_cancelled)

        self.contract_selection.set_parent_window(self.content.get_parent_window().get_user_data())
        merged_pools.sort_virt_to_top()

        for pool in merged_pools.pools:
            self.contract_selection.add_pool(pool, quantity_to_consume)

        self.contract_selection.show()

    def _selection_callback(self, treeselection):
        model, tree_iter = treeselection.get_selected()
        if model.iter_n_children(tree_iter) > 0:
            self.sub_details.clear()
            self.on_no_selection()
        else:
            widgets.SubscriptionManagerTab._selection_callback(self, treeselection)

    def on_selection(self, selection):
        """ Shows details for the current selected pool. """
        if selection.is_valid():
            product_name = selection['product_name']
            pool_id = selection['pool_id']
            support_level = selection['support_level']
            support_type = selection['support_type']
            provided = self.pool_stash.lookup_provided_products(pool_id)

            self.sub_details.show(product_name, products=provided,
                    highlight=self.get_filter_text(),
                    support_level=support_level, support_type=support_type,
                    sku=selection['product_id'])
        else:
            self.sub_details.clear()

        self.subscribe_button.set_sensitive(selection.is_valid())

    def on_no_selection(self):
        self.subscribe_button.set_sensitive(False)

    def calculate_default_quantity(self, pool):
        try:
            return int(pool['calculatedAttributes']['suggested_quantity'])
        except:
            return 1
