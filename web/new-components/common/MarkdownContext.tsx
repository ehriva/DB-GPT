import markdownComponents, { markdownPlugins, preprocessLaTeX } from '@/components/chat/chat-content/config';
import { GPTVis } from '@antv/gpt-vis';
import React from 'react';
import { useTranslation } from 'react-i18next';
import MarkdownErrorBoundary from './MarkdownErrorBoundary';

const MarkdownContent: React.FC<{ children: string }> = ({ children }) => {
  return (
    <GPTVis components={{ ...markdownComponents }} {...markdownPlugins}>
      {preprocessLaTeX(children)}
    </GPTVis>
  );
};

const MarkDownContext: React.FC<{ children: string }> = ({ children }) => {
  const { t } = useTranslation();
  return (
    <MarkdownErrorBoundary
      key={children}
      text={children}
      errorLabel={t('reply_render_failed')}
      retryLabel={t('retry_reply_render')}
    >
      <MarkdownContent>{children}</MarkdownContent>
    </MarkdownErrorBoundary>
  );
};

export default MarkDownContext;
