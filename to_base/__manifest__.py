{
    'name': "Viindoo Base",
    'name_vi_VN': "Mô-đun Cơ Sở",

    'summary': """
Additional tools and utilities for other modules""",
    'summary_vi_VN': """
Bổ sung các công cụ và tiện ích cho các mô-đun khác""",

    'description': """
Base module that provides additional tools and utilities for developers

* Check if barcode exist by passing model and barcode field name
* Generate barcode from any number
* Find the IP of the host where Odoo is running.
* Date & Time Utilities

  * Convert time to UTC
  * UTC to local time
  * Get weekdays for a given period
  * Same Weekday next week
  * Split date

* Zip a directory and return bytes object which is ready for storing in Binary fields. No on-disk temporary file is needed.

  * usage: zip_archive_bytes = self.env['to.base'].zip_dir(path_to_directory_to_zip)

* Sum all digits of a number (int|float)
* Finding the lucky number (digit sum = 9) which is nearest the given number
* Return remote host IP by sending http request to http(s)://base_url/my/ip/

Editions Supported
==================
1. Community Edition
2. Enterprise Edition

    """,

    'description_vi_VN': """
Mô-đun cơ sở cung cấp các công cụ và tiện ích bổ sung cho các nhà phát triển

* Kiểm tra xem mã vạch có tồn tại hay không bằng cách đi qua mô hình và tên trường mã vạch
* Tạo mã vạch từ bất kỳ số nào
* Tìm IP của máy chủ lưu trữ Odoo đang chạy.
* Tiện ích Ngày & Giờ

  * Chuyển đổi thời gian sang UTC
  * UTC sang giờ địa phương
  * Lấy các ngày trong tuần trong một khoảng thời gian nhất định
  * Cùng ngày trong tuần vào tuần tới
  * Tách ngày

* Zip một thư mục và trả về đối tượng byte đã sẵn sàng để lưu trữ trong các trường Nhị phân. Không cần tệp tạm thời trên đĩa.

  * sử dụng: zip_archive_bytes = self.env['to.base'].zip_dir(path_to_directory_to_zip)

* Tính tổng tất cả các chữ số của một số (int | float)
* Tìm số may mắn (tổng = 9) gần nhất với số đã cho
* Trả lại IP máy chủ từ xa bằng cách gửi yêu cầu http đến http (s): // base_url / my / ip /

Ấn bản được Hỗ trợ
==================
1. Ấn bản Community
2. Ấn bản Enterprise

    """,

    'author': "T.V.T Marine Automation (aka TVTMA),Viindoo",
    'website': 'https://viindoo.com/apps/modules/19.0/to_base',
    'live_test_url': "https://v19demo-int.viindoo.com/web#action=base.open_module_tree",
    'live_test_url_vi_VN': "https://v19demo-vn.viindoo.com/web#action=base.open_module_tree",
    'support': 'apps.support@viindoo.com',

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/master/odoo/addons/base/module/module_data.xml
    # for the full list
    'category': 'Technical Settings',
    'version': '0.6.0',

    # any module necessary for this one to work correctly
    'depends': [
        'web',
        'base_setup',
        'mail'
        ],

    # always loaded
    'data': [
        'data/base_data.xml',
        ],
    'images': ['static/description/main_screenshot.png'],
    # No assets: the resizable search panel this module used to patch in is a
    # NATIVE feature of Odoo 19 core (.o_search_panel_resize in
    # web/static/src/search/search_panel/search_panel.scss), so the patch was
    # dropped on the 19.0 port (0d2dc5b56a); the favicon/settings-page assets
    # went to viin_brand with the branding mechanism.
    'demo': [
        'data/res_partner_category_demo_data.xml',
        'data/res_partner_demo_data.xml',
        ],
    'installable': True,
    'auto_install': ['web'],
    'price': 4.95,
    'currency': 'EUR',
    'license': 'OPL-1',
}
