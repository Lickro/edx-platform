/* globals $ */
import 'jquery.cookie';

export class LatestUpdate {  // eslint-disable-line import/prefer-default-export

  constructor(options) {
    if ($.cookie('update-message') == 'hide') {
      $('.update-message').hide();
    }
    $('.dismiss-message button').click(() => {
        $.cookie('update-message', 'hide', { expires: 1 });
        $('.update-message').hide();
    });
  }
}
