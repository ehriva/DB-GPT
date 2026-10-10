import { IFlowNodeParameter } from '@/types/flow';
import { convertKeysToCamelCase } from '@/utils/flow';
import * as Icons from '@ant-design/icons';
import { Input } from 'antd';

const getIconComponent = (iconString: string) => {
  const match = iconString.match(/^icon:(\w+)$/);
  if (match) {
    const iconName = match[1] as keyof typeof Icons;
    if (!/(Outlined|Filled|TwoTone)$/.test(iconName)) return null;
    const candidate = Icons[iconName];
    if (!candidate || typeof candidate !== 'object' || !('$$typeof' in candidate)) return null;
    const IconComponent = candidate as typeof Icons.InfoCircleOutlined;
    return <IconComponent />;
  }
  return null;
};

export const renderInput = (data: IFlowNodeParameter) => {
  const attr = convertKeysToCamelCase(data.ui?.attr || {});
  attr.prefix = getIconComponent(data.ui?.attr?.prefix || '');

  return <Input {...attr} className='w-full' placeholder='please input' allowClear />;
};
